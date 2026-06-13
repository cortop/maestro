"""Spawning and listing per-ticket reconciler sessions.

A reconciler is a *top-level* ``claude --bg`` session (NOT a subagent) — that is the
only fan-out unit on the harness that keeps the ability to spawn its own subagents
(the Implementer/QA pair). One Workflow session would be a single failure domain for
all tickets; N independent OS sessions are not.

This module is an abstraction so the dispatcher can be unit-tested with a fake and
so the exact ``claude`` CLI surface is isolated in one place.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Protocol

RECONCILE_PREFIX = "reconcile-"


def session_name(key: str) -> str:
    return f"{RECONCILE_PREFIX}{key}"


def key_from_session(name: str) -> str | None:
    return name[len(RECONCILE_PREFIX):] if name.startswith(RECONCILE_PREFIX) else None


class SessionManager(Protocol):
    def list_active(self) -> set[str]:
        """Names of currently-working reconciler sessions (the 'already claimed' set)."""

    def spawn(self, key: str, prompt: str, cwd: Path) -> None:
        """Launch a detached reconciler session for ``key``."""


class ClaudeCliSessions:
    """Real implementation backed by the ``claude`` CLI."""

    def __init__(self, model: str = "sonnet"):
        self.model = model

    def list_active(self) -> set[str]:
        try:
            out = subprocess.run(
                ["claude", "agents", "list", "--json"],
                capture_output=True, text=True, timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return set()
        if out.returncode != 0 or not out.stdout.strip():
            return set()
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            return set()
        rows = data.get("agents", data) if isinstance(data, dict) else data
        active: set[str] = set()
        for r in rows or []:
            name = (r.get("name") or "") if isinstance(r, dict) else ""
            status = (r.get("status") or "").lower() if isinstance(r, dict) else ""
            if name.startswith(RECONCILE_PREFIX) and status not in {"done", "stopped", "failed", "exited"}:
                active.add(name)
        return active

    def spawn(self, key: str, prompt: str, cwd: Path) -> None:
        cmd = [
            "claude", "--bg", "--name", session_name(key),
            "--model", self.model,
            "-p", prompt,
        ]
        subprocess.Popen(
            cmd, cwd=str(cwd),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


class DryRunSessions:
    """Records spawns instead of launching anything (for --dry-run and tests)."""

    def __init__(self, active: set[str] | None = None):
        self._active = set(active or set())
        self.spawned: list[tuple[str, str, str]] = []

    def list_active(self) -> set[str]:
        return set(self._active)

    def spawn(self, key: str, prompt: str, cwd: Path) -> None:
        self.spawned.append((key, prompt, str(cwd)))
        self._active.add(session_name(key))
