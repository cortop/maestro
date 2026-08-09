"""CLI-backed provider adapters. Thin shells over ``jira`` / ``gh`` / a custom import
command. All project-specific values come from ``config.toml`` — nothing hardcoded.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess


def _run(cmd: list[str], timeout: int = 60, env: dict | None = None) -> tuple[int, str, str]:
    """*env* (GA-17) is a credential overlay (``dispatcher.resolve_credential``)
    to run *cmd* under instead of the ambient environment. None (the default)
    is byte-identical to before this ticket -- ``subprocess.run(env=None)``
    inherits the parent process's environment exactly as a bare call did."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        # The executable itself is missing (e.g. `gh` not installed) -- a config
        # problem, not something a retry fixes. Sentinel rc=127 (shell convention
        # for "command not found") plus distinguishing text; `classify_gh_failure`
        # keys primarily on the text so this stays symmetric with real `gh` output.
        return 127, "", f"maestro: executable not found: {e}"
    except subprocess.TimeoutExpired as e:
        # The process itself never returned -- a transient/retryable condition
        # (network blip, slow host), unlike a FileNotFoundError. Sentinel rc=124
        # (shell convention for "command timed out").
        return 124, "", f"maestro: command timed out after {timeout}s: {e}"


# Verbatim substrings from real `gh` 2.94.0 stderr (measured, not guessed) --
# matched lowercase. Order matters: SAML enforcement reads like a 403/not-found
# to a human but is an auth problem (the token needs the org's blessing), so the
# auth check must win over any accidental "not found" phrasing.
_AUTH_MARKERS = (
    "bad credentials",
    "must grant your oauth token access",
    "protected by organization saml enforcement",
    "executable not found",  # gh itself missing: a human must fix the environment
)
_NOT_FOUND_MARKERS = (
    "could not resolve to a repository",
    "could not resolve to a pullrequest",
)
_TRANSIENT_MARKERS = (
    "timed out",
    "connection reset",
    "could not resolve host",
    "temporary failure in name resolution",
)


def classify_gh_failure(rc: int, stdout: str, stderr: str) -> str:
    """Classify a failed `gh` invocation into "auth" | "not_found" | "transient"
    | "unknown", from stderr TEXT -- real `gh` exits 1 for auth failures,
    not-found errors, and everything else alike, so the exit code alone can
    never be the signal (`rc`/`stdout` are accepted for a stable signature and
    future use, but only `stderr` text is matched today). Unmatched text
    degrades to "unknown" -- today's behavior -- rather than a guess.
    """
    text = stderr.lower()
    if any(m in text for m in _AUTH_MARKERS):
        return "auth"
    if any(m in text for m in _NOT_FOUND_MARKERS):
        return "not_found"
    if any(m in text for m in _TRANSIENT_MARKERS):
        return "transient"
    return "unknown"


class JiraCliTracker:
    def __init__(self, settings: dict):
        self.project_key = settings.get("project_key", "")

    def view(self, key: str) -> dict:
        rc, out, _ = _run(["jira", "issue", "view", key, "--raw"])
        if rc == 0 and out.strip():
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                return {"plain": out}
        return {}

    def transition(self, key: str, status: str) -> None:
        _run(["jira", "issue", "move", key, status])

    def assignee(self, key: str) -> str | None:
        data = self.view(key)
        try:
            return data["fields"]["assignee"]["displayName"]
        except (KeyError, TypeError):
            return None

    def import_new(self, home) -> int:
        return 0  # sync via the `jira` CLI is out of scope; see JiraTracker for REST-based sync

    def refresh(self, home, key: str, external_id: str) -> int:
        return 0


class GitHubCliVCS:
    def __init__(self, settings: dict):
        self.repos = settings.get("repos", [])

    def pr_for_branch(self, branch: str, repo: str | None = None,
                      env: dict | None = None) -> dict | None:
        for r in [repo] if repo else (self.repos or [None]):
            cmd = ["gh", "pr", "list", "--head", branch, "--state", "all",
                   "--json", "number,url,isDraft,state,mergeStateStatus"]
            if r:
                cmd += ["--repo", r]
            rc, out, _ = _run(cmd, env=env)
            if rc == 0 and out.strip():
                rows = json.loads(out)
                if rows:
                    return rows[0]
        return None

    def pr_status(self, pr_number: int, repo: str | None = None,
                  env: dict | None = None) -> dict:
        repo = repo or (self.repos[0] if self.repos else None)
        cmd = ["gh", "pr", "view", str(pr_number), "--json",
               "state,mergeable,headRefOid,statusCheckRollup"]
        if repo:
            cmd += ["--repo", repo]
        rc, out, err = _run(cmd, env=env)
        if rc != 0 or not out.strip():
            return {"state": "unknown", "mergeable": "UNKNOWN", "head_sha": None,
                    "ci_state": "unknown", "failing_checks": [],
                    "error": classify_gh_failure(rc, out, err)}
        data = json.loads(out)
        checks = data.get("statusCheckRollup") or []
        failing, pending = [], False
        for c in checks:
            name = c.get("name") or c.get("context") or "check"
            conclusion = (c.get("conclusion") or c.get("state") or "").upper()
            status = (c.get("status") or "").upper()
            if conclusion in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"}:
                failing.append(name)
            elif not conclusion and status in {"IN_PROGRESS", "QUEUED", "PENDING"}:
                pending = True
            elif conclusion == "PENDING":
                pending = True
        if failing:
            ci_state = "failing"
        elif pending:
            ci_state = "pending"
        elif checks:
            ci_state = "passing"
        else:
            ci_state = "unknown"
        return {
            "state": data.get("state", "unknown"),
            "mergeable": data.get("mergeable", "UNKNOWN"),
            "head_sha": data.get("headRefOid"),
            "ci_state": ci_state,
            "failing_checks": failing,
        }

    def review_feedback(self, pr_number: int, repo: str | None = None,
                        env: dict | None = None) -> list[dict]:
        repo = repo or (self.repos[0] if self.repos else None)
        cmd = ["gh", "pr", "view", str(pr_number), "--json", "reviews"]
        if repo:
            cmd += ["--repo", repo]
        rc, out, _ = _run(cmd, env=env)
        if rc != 0 or not out.strip():
            return []
        reviews = json.loads(out).get("reviews") or []
        result = []
        for r in reviews:
            rid = r.get("id")
            if not rid:
                continue
            result.append({
                "id": str(rid),
                "state": r.get("state"),
                "body": r.get("body", ""),
                "author": (r.get("author") or {}).get("login"),
            })
        return result


class CommandFetcher:
    """Runs an arbitrary import command (e.g. the old helsinki.sh) that is expected
    to enqueue create-requests via `maestro create`. Fully project-defined."""

    def __init__(self, settings: dict):
        self.cmd = settings.get("cmd", "")

    def fetch(self) -> int:
        if not self.cmd:
            return 0
        rc, out, _ = _run(["bash", "-lc", os.path.expanduser(self.cmd)], timeout=600)
        # The command reports the count it enqueued on its last stdout line.
        try:
            return int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0
