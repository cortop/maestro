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

    def ci_state(self, pr_number: int) -> str:
        repo = self.repos[0] if self.repos else None
        cmd = ["gh", "pr", "checks", str(pr_number), "--json", "state"]
        if repo:
            cmd += ["--repo", repo]
        rc, out, _ = _run(cmd)
        if rc != 0 or not out.strip():
            return "unknown"
        states = [r.get("state", "") for r in json.loads(out)]
        if any(s in {"FAILURE", "ERROR"} for s in states):
            return "failing"
        if any(s in {"PENDING", "IN_PROGRESS", "QUEUED"} for s in states):
            return "pending"
        return "passing" if states else "unknown"


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
