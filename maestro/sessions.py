"""Spawning and listing per-ticket reconciler sessions.

A reconciler is a detached, headless ``claude -p "/maestro-reconcile <KEY>"`` process —
a TOP-LEVEL session (not a subagent), so it keeps the ``Agent`` tool and can run the
Implementer/QA pair. Liveness is tracked via :mod:`maestro.claims` (pid files), because
headless sessions don't show up in ``claude agents --json``.

``SessionManager.list_active`` and ``spawn`` speak in terms of ticket KEYS.
``list_sessions`` enumerates captured log files for a key, sorted newest-first.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Protocol

from . import claims, store

RECONCILE_PREFIX = "reconcile-"

# Filename pattern: reconcile-<KEY>-<epoch>.{log,stream.jsonl}
_SESSION_FILE_RE = re.compile(
    r"^(reconcile-(?P<key>.+?)-(?P<epoch>\d+\.\d+))\.(?P<ext>log|stream\.jsonl)$"
)


def list_sessions(home: Path, key: str, *, with_outcome: bool = False) -> list[dict]:
    """Return session log metadata for *key*, newest-first.

    Each dict has: ``session_id``, ``path``, ``format`` ('text'|'stream-json'),
    ``epoch`` (float), ``ts`` (ISO string). ``with_outcome`` is opt-in — it tail-scans
    each log via :func:`maestro.steplog.session_outcome` to add an ``outcome`` field, so
    the default (filename-only) call opens no log files, keeping callers like
    ``ops.prune_session_logs`` and the TUI's log tailer cheap.
    """
    store.validate_key(key)
    log_dir = home / "agent-logs" / key
    if not log_dir.exists():
        return []
    out = []
    for f in log_dir.iterdir():
        m = _SESSION_FILE_RE.match(f.name)
        if not m:
            continue
        epoch = float(m.group("epoch"))
        fmt = "stream-json" if m.group("ext") == "stream.jsonl" else "text"
        entry = {
            "session_id": m.group(1),
            "path": str(f),
            "format": fmt,
            "epoch": epoch,
            "ts": _epoch_to_iso(epoch),
        }
        if with_outcome:
            from . import steplog
            entry["outcome"] = steplog.session_outcome(f)["outcome"]
        out.append(entry)
    out.sort(key=lambda d: d["epoch"], reverse=True)
    return out


def _epoch_to_iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def session_name(key: str) -> str:
    return f"{RECONCILE_PREFIX}{key}"


class SessionManager(Protocol):
    def list_active(self) -> set[str]:
        """Keys with a live reconciler (the 'already claimed' set)."""

    def spawn(self, key: str, prompt: str, cwd: Path,
              model: str | None = None, effort: str | None = None) -> int | None:
        """Launch a detached reconciler for ``key``; return its pid (or None).

        *model* and *effort* override instance defaults when provided.
        """


class ClaudeCliSessions:
    """Real implementation: detached ``claude -p`` + a pid claim file."""

    def __init__(self, home: Path, model: str = "sonnet",
                 permission_mode: str | None = "acceptEdits",
                 extra_args: list[str] | None = None,
                 capture_session_logs: bool = True,
                 session_log_format: str = "stream-json",
                 clock: Callable[[], float] | None = None):
        self.home = Path(home)
        self.model = model
        self.permission_mode = permission_mode
        self.extra_args = extra_args or []
        self.capture_session_logs = capture_session_logs
        self.session_log_format = session_log_format
        self._clock: Callable[[], float] = clock or store.now_epoch

    def list_active(self) -> set[str]:
        return claims.active_keys(self.home)

    def spawn(self, key: str, prompt: str, cwd: Path,
              model: str | None = None, effort: str | None = None) -> int | None:
        session_id = f"{session_name(key)}-{self._clock():.6f}"
        effective_model = model or self.model
        cmd = ["claude", "-p", prompt, "--model", effective_model, "-n", session_name(key)]
        if effort:
            cmd += ["--effort", effort]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        cmd += self.extra_args
        env = dict(os.environ)
        env["MAESTRO_HOME"] = str(self.home)  # pin the home for the worker

        log_path: str | None = None
        if self.capture_session_logs:
            if self.session_log_format == "stream-json":
                cmd += ["--output-format", "stream-json", "--verbose"]
                log_file = store.session_stream_path(self.home, key, session_id)
            else:
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
        self.spawned: list[tuple[str, str, str, str | None, str | None]] = []

    def list_active(self) -> set[str]:
        return set(self._active)

    def spawn(self, key: str, prompt: str, cwd: Path,
              model: str | None = None, effort: str | None = None) -> int | None:
        self.spawned.append((key, prompt, str(cwd), model, effort))
        self._active.add(key)
        return None
