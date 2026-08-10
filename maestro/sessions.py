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

# Filename pattern: reconcile-<KEY>-<epoch>.{log,stream.jsonl,opencode.jsonl}
_SESSION_FILE_RE = re.compile(
    r"^(reconcile-(?P<key>.+?)-(?P<epoch>\d+\.\d+))\.(?P<ext>log|stream\.jsonl|opencode\.jsonl)$"
)

# Maps a matched filename ``ext`` to the ``format`` value reported to callers. Any
# ``ext`` not listed here falls back to "text" -- there is currently no such case
# (the regex's alternation is closed), but the fallback keeps this mapping additive
# if a future format slot lands.
_EXT_TO_FORMAT = {
    "stream.jsonl": "stream-json",
    "opencode.jsonl": "opencode",
}


def list_sessions(home: Path, key: str, *, with_outcome: bool = False) -> list[dict]:
    """Return session log metadata for *key*, newest-first.

    Each dict has: ``session_id``, ``path``, ``format`` ('text'|'stream-json'|
    'opencode'), ``epoch`` (float), ``ts`` (ISO string). ``with_outcome`` is opt-in —
    it tail-scans each log via :func:`maestro.steplog.session_outcome` to add an
    ``outcome`` field, so the default (filename-only) call opens no log files, keeping
    callers like ``ops.prune_session_logs`` and the TUI's log tailer cheap.

    'opencode' (RF-3) is a non-Claude runner's own log grammar -- it is deliberately
    never "stream-json" (``ratelimit.probe`` / ``spend.probe`` gate on that exact
    string to skip logs they can't parse) and every consumer above
    :func:`maestro.steplog.iter_records` that isn't format-aware already treats
    anything other than "stream-json" as opaque text, so it falls straight into the
    existing plain-text render/prune/outcome fallbacks.
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
        fmt = _EXT_TO_FORMAT.get(m.group("ext"), "text")
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

    def spawn(self, key: str, command: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None,
              env_overlay: dict[str, str] | None = None,
              runner: str | None = None) -> int | None:
        """Launch a detached reconciler for ``key``; return its pid (or None).

        *command* (RF-1) is the resolved reconcile slash-command (see
        ``dispatcher.resolve_reconcile_command``, e.g. ``/maestro-reconcile-implementing``)
        WITHOUT the key appended -- the dispatcher passes ``command`` and ``key`` as separate
        arguments rather than pre-flattening them into one prompt string, so each backend
        composes its own invocation. ``ClaudeCliSessions.spawn`` composes the identical
        ``f"{command} {key}"`` prompt it always has (argv unchanged); a non-Claude backend
        (e.g. opencode) can instead take them as a distinct ``--command <name>`` flag plus
        argument, without string-parsing a slash command back out of an already-flattened
        prompt.

        *model* and *effort* override instance defaults when provided.
        *disallowed_tools* is the per-tier tool-surface denylist (see
        ``dispatcher.tier_denylist``) rendered as a ``--disallowedTools`` flag.
        *allowed_tools* (GA-10) is the per-key --allowedTools additions
        (``dispatcher.resolved_allowed_tools`` -- the board-wide
        ``reconcile_allowed_tools`` list unioned with the resolved repo binding's
        own list); the implementation merges this with its own process-wide base
        grant (maestro CLI verbs + reconcile_web_tools) into exactly ONE
        ``--allowedTools`` flag, never two.
        *env_overlay* (GA-17) is this key's resolved gh credential (see
        ``maestro.credentials`` / ``dispatcher.resolve_credential``) -- merged
        into the spawned session's env beside the ``MAESTRO_HOME`` pin. None
        (the default -- no credential configured for this key's repo) leaves
        the env byte-identical to before this ticket.

        *runner* (RF-2) is the resolved runner name (``dispatcher.resolve_runner``,
        already validated as registered by the caller) -- an implementation that
        only ever speaks one backend (e.g. ``ClaudeCliSessions``) may ignore it;
        ``RoutingSessions`` uses it to pick which delegate's ``spawn`` to call.
        None means "the default runner" (``"claude"``).
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

    def spawn(self, key: str, command: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None,
              env_overlay: dict[str, str] | None = None,
              runner: str | None = None) -> int | None:
        # RF-2: this backend only ever speaks Claude -- `runner` is accepted (so
        # callers can pass it uniformly, e.g. via RoutingSessions) but otherwise
        # unused; the caller (dispatcher.dispatch) has already validated it's
        # either None or "claude" before routing a spawn to this instance.
        del runner
        session_id = f"{session_name(key)}-{self._clock():.6f}"
        effective_model = model or self.model
        # RF-1: compose the flattened prompt here, from the separate command/key
        # inputs -- identical to the string the caller used to pre-flatten, so argv
        # is unchanged.
        prompt = f"{command} {key}"
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
        # GA-17: this key's resolved gh credential wins over whatever's ambient --
        # it's an explicit, already-fail-closed-checked resolution, not a guess.
        if env_overlay:
            env.update(env_overlay)

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
                           log_path=log_path, cwd=str(cwd), prompt=prompt)
        return proc.pid


class DryRunSessions:
    """Records spawns instead of launching (for --dry-run and tests)."""

    def __init__(self, active: set[str] | None = None):
        self._active = set(active or set())   # KEYS
        # 9-tuple: (key, prompt, cwd, model, effort, disallowed_tools, allowed_tools,
        # env_overlay, runner). GA-10 appended allowed_tools as the 7th element; GA-17
        # appended env_overlay as the 8th; RF-2 appends runner as the 9th -- the SAME
        # way -- any later per-key spawn input appends here too, rather than opening a
        # second per-key channel. Unpack by name or by negative index, never assume
        # this stays exactly 9 long. RF-1: spawn()'s own 2nd parameter is now
        # ``command`` (no key appended) -- this ``prompt`` element is the composed
        # "<command> <key>" string built inside spawn() itself, so existing readers of
        # element 1 see the same value as before this ticket.
        self.spawned: list[tuple[str, str, str, str | None, str | None, list[str], list[str],
                                 dict[str, str], str]] = []

    def list_active(self) -> set[str]:
        return set(self._active)

    def spawn(self, key: str, command: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None,
              env_overlay: dict[str, str] | None = None,
              runner: str | None = None) -> int | None:
        prompt = f"{command} {key}"
        self.spawned.append((key, prompt, str(cwd), model, effort,
                             list(disallowed_tools or []), list(allowed_tools or []),
                             dict(env_overlay or {}), runner or "claude"))
        self._active.add(key)
        return None


class RoutingSessions:
    """A ``SessionManager`` that dispatches ``spawn`` to one of several backend
    delegates by runner name (RF-2) -- ``{runner: SessionManager}``. This is the
    ONE place a resolved ``runner`` (``dispatcher.resolve_runner``, already
    validated as registered by the caller -- routing to an unregistered name is
    a caller bug, not a runtime condition this class handles gracefully) turns
    into an actual backend call; both construction sites (``cli.py``'s
    ``_nudge`` and ``cmd_dispatch``) build one of these instead of a bare
    ``ClaudeCliSessions`` now, so a second backend registers by adding one more
    delegate here, not by touching either call site again.

    ``list_active`` asks exactly ONE delegate (the first registered) instead of
    every delegate and merging -- ``claims`` tracks liveness runner-agnostically
    (pid + start_epoch only, never the spawned command string, see
    ``claims._verdict``), so every delegate sharing the same home would compute
    the identical set; asking N of them would just re-read (and, for
    ``ClaudeCliSessions``, re-verify via ``ps``) the same claim files N times.
    """

    def __init__(self, delegates: dict[str, SessionManager]):
        if not delegates:
            raise store.MaestroError("RoutingSessions needs at least one delegate")
        self.delegates = dict(delegates)

    def list_active(self) -> set[str]:
        return next(iter(self.delegates.values())).list_active()

    def spawn(self, key: str, command: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None,
              env_overlay: dict[str, str] | None = None,
              runner: str | None = None) -> int | None:
        name = runner or "claude"
        try:
            delegate = self.delegates[name]
        except KeyError:
            raise store.MaestroError(
                f"RoutingSessions: no delegate registered for runner {name!r} "
                f"(registered: {sorted(self.delegates)}) -- the caller must "
                "validate the runner before calling spawn") from None
        return delegate.spawn(key, command, cwd, model=model, effort=effort,
                              disallowed_tools=disallowed_tools, allowed_tools=allowed_tools,
                              env_overlay=env_overlay, runner=name)
