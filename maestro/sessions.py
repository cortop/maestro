"""Spawning and listing per-ticket reconciler sessions.

A reconciler is a detached, headless ``claude -p "/maestro-reconcile <KEY>"`` process —
a TOP-LEVEL session (not a subagent), so it keeps the ``Agent`` tool and can run the
Implementer/QA pair. Liveness is tracked via :mod:`maestro.claims` (pid files), because
headless sessions don't show up in ``claude agents --json``.

``SessionManager.list_active`` and ``spawn`` speak in terms of ticket KEYS.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Protocol

from . import claims

RECONCILE_PREFIX = "reconcile-"


def session_name(key: str) -> str:
    return f"{RECONCILE_PREFIX}{key}"


class SessionManager(Protocol):
    def list_active(self) -> set[str]:
        """Keys with a live reconciler (the 'already claimed' set)."""

    def spawn(self, key: str, prompt: str, cwd: Path) -> int | None:
        """Launch a detached reconciler for ``key``; return its pid (or None)."""


class ClaudeCliSessions:
    """Real implementation: detached ``claude -p`` + a pid claim file."""

    def __init__(self, home: Path, model: str = "sonnet",
                 permission_mode: str | None = "acceptEdits",
                 extra_args: list[str] | None = None):
        self.home = Path(home)
        self.model = model
        self.permission_mode = permission_mode
        self.extra_args = extra_args or []

    def list_active(self) -> set[str]:
        return claims.active_keys(self.home)

    def spawn(self, key: str, prompt: str, cwd: Path) -> int | None:
        cmd = ["claude", "-p", prompt, "--model", self.model, "-n", session_name(key)]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        cmd += self.extra_args
        env = dict(os.environ)
        env["MAESTRO_HOME"] = str(self.home)  # pin the home for the worker
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        claims.write_claim(self.home, key, proc.pid, session_name(key))
        return proc.pid


class DryRunSessions:
    """Records spawns instead of launching (for --dry-run and tests)."""

    def __init__(self, active: set[str] | None = None):
        self._active = set(active or set())   # KEYS
        self.spawned: list[tuple[str, str, str]] = []

    def list_active(self) -> set[str]:
        return set(self._active)

    def spawn(self, key: str, prompt: str, cwd: Path) -> int | None:
        self.spawned.append((key, prompt, str(cwd)))
        self._active.add(key)
        return None
