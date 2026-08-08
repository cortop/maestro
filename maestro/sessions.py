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
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None) -> int | None:
        """Launch a detached reconciler for ``key``; return its pid (or None).

        *model* and *effort* override instance defaults when provided.
        *disallowed_tools* is the per-tier tool-surface denylist (see
        ``dispatcher.tier_denylist``) rendered as a ``--disallowedTools`` flag.
        *allowed_tools* (GA-10) is the per-key --allowedTools additions
        (``dispatcher.resolved_allowed_tools`` -- the board-wide
        ``reconcile_allowed_tools`` list unioned with the resolved repo binding's
        own list); the implementation merges this with its own process-wide base
        grant (maestro CLI verbs + reconcile_web_tools) into exactly ONE
        ``--allowedTools`` flag, never two.
        """


class ClaudeCliSessions:
    """Real implementation: detached ``claude -p`` + a pid claim file."""

    def __init__(self, home: Path, model: str = "sonnet",
                 permission_mode: str | None = "acceptEdits",
                 extra_args: list[str] | None = None,
                 base_allowed_tools: list[str] | None = None,
                 capture_session_logs: bool = True,
                 session_log_format: str = "stream-json",
                 clock: Callable[[], float] | None = None,
                 unverified_claim_max_age: float = claims.DEFAULT_UNVERIFIED_CLAIM_MAX_AGE,
                 claims_run=subprocess.run):
        self.home = Path(home)
        self.model = model
        self.permission_mode = permission_mode
        self.extra_args = extra_args or []
        # GA-10: the process-wide "always-on" --allowedTools rules (maestro CLI verbs +
        # reconcile_web_tools, see cli._reconciler_tool_grants) -- bare rules, not a
        # pre-built "--allowedTools <value>" pair, so spawn() can merge in the per-key
        # allowed_tools argument and still emit exactly ONE --allowedTools flag.
        self.base_allowed_tools = base_allowed_tools or []
        self.capture_session_logs = capture_session_logs
        self.session_log_format = session_log_format
        self._clock: Callable[[], float] = clock or store.now_epoch
        self._unverified_claim_max_age = unverified_claim_max_age
        self._claims_run = claims_run

    def list_active(self) -> set[str]:
        return claims.active_keys(self.home, run=self._claims_run,
                                  max_age=self._unverified_claim_max_age)

    def spawn(self, key: str, prompt: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None) -> int | None:
        session_id = f"{session_name(key)}-{self._clock():.6f}"
        effective_model = model or self.model
        cmd = ["claude", "-p", prompt, "--model", effective_model, "-n", session_name(key)]
        if effort:
            cmd += ["--effort", effort]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if disallowed_tools:
            cmd += ["--disallowedTools", ",".join(disallowed_tools)]
        # GA-10: merge the process-wide base grant with this key's per-repo/board-wide
        # additions into exactly ONE --allowedTools flag -- never two (an "eval" wrapped
        # around a second flag, or a genuinely duplicated flag, would silently let the
        # LAST one win and could widen the grant unintentionally).
        merged_allowed: list[str] = list(self.base_allowed_tools)
        for tool in allowed_tools or []:
            if tool not in merged_allowed:
                merged_allowed.append(tool)
        assert "--allowedTools" not in self.extra_args, \
            "extra_args must never carry --allowedTools -- use base_allowed_tools instead"
        if merged_allowed:
            cmd += ["--allowedTools", ",".join(merged_allowed)]
        cmd += self.extra_args
        assert cmd.count("--allowedTools") <= 1, "spawn argv must carry at most one --allowedTools flag"
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
        # 7-tuple: (key, prompt, cwd, model, effort, disallowed_tools, allowed_tools).
        # GA-10 appended allowed_tools LAST, after the prior 6-tuple shape -- any later
        # per-key spawn input (e.g. GA-17's env overlay) appends here too, rather than
        # opening a second per-key channel. Unpack by name or by negative index, never
        # assume this stays exactly 7 long.
        self.spawned: list[tuple[str, str, str, str | None, str | None, list[str], list[str]]] = []

    def list_active(self) -> set[str]:
        return set(self._active)

    def spawn(self, key: str, prompt: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None) -> int | None:
        self.spawned.append((key, prompt, str(cwd), model, effort,
                             list(disallowed_tools or []), list(allowed_tools or [])))
        self._active.add(key)
        return None
