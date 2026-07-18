"""CLI-backed provider adapters. Thin shells over ``jira`` / ``gh`` / a custom import
command. All project-specific values come from ``config.toml`` — nothing hardcoded.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


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

    def pr_for_branch(self, branch: str) -> dict | None:
        for repo in self.repos or [None]:
            cmd = ["gh", "pr", "list", "--head", branch, "--state", "all",
                   "--json", "number,url,isDraft,state,mergeStateStatus"]
            if repo:
                cmd += ["--repo", repo]
            rc, out, _ = _run(cmd)
            if rc == 0 and out.strip():
                rows = json.loads(out)
                if rows:
                    return rows[0]
        return None

    def pr_status(self, pr_number: int) -> dict:
        repo = self.repos[0] if self.repos else None
        cmd = ["gh", "pr", "view", str(pr_number), "--json",
               "state,mergeable,headRefOid,statusCheckRollup"]
        if repo:
            cmd += ["--repo", repo]
        rc, out, _ = _run(cmd)
        if rc != 0 or not out.strip():
            return {"state": "unknown", "mergeable": "UNKNOWN", "head_sha": None,
                    "ci_state": "unknown", "failing_checks": []}
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

    def review_feedback(self, pr_number: int) -> list[dict]:
        repo = self.repos[0] if self.repos else None
        cmd = ["gh", "pr", "view", str(pr_number), "--json", "reviews"]
        if repo:
            cmd += ["--repo", repo]
        rc, out, _ = _run(cmd)
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
