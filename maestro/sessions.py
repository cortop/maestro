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
from typing import Callable, Protocol

from . import claims, store

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
                 extra_args: list[str] | None = None,
                 capture_session_logs: bool = True,
                 clock: Callable[[], float] | None = None):
        self.home = Path(home)
        self.model = model
        self.permission_mode = permission_mode
        self.extra_args = extra_args or []
        self.capture_session_logs = capture_session_logs
        self._clock: Callable[[], float] = clock or store.now_epoch

    def list_active(self) -> set[str]:
        return claims.active_keys(self.home)

    def spawn(self, key: str, prompt: str, cwd: Path) -> int | None:
        session_id = f"{session_name(key)}-{self._clock():.6f}"
        cmd = ["claude", "-p", prompt, "--model", self.model, "-n", session_name(key)]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        cmd += self.extra_args
        env = dict(os.environ)
        env["MAESTRO_HOME"] = str(self.home)  # pin the home for the worker

        log_path: str | None = None
        if self.capture_session_logs:
            log_file = store.session_log_path(self.home, key, session_id)
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_file.open("w", encoding="utf-8")
            log_path = str(log_file)
        else:
            log_handle = None

        try:
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle if log_handle is not None else subprocess.DEVNULL,
                stderr=log_handle if log_handle is not None else subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            if log_handle is not None:
                log_handle.close()

        claims.write_claim(self.home, key, proc.pid, session_name(key),
                           log_path=log_path)
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
