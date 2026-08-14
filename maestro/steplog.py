"""Step-log folder — reads a session log (Claude ``.stream.jsonl``, opencode
``.opencode.jsonl``, or pi ``.pi.jsonl``) and appends IMPL_STEP events.

Extracts notable tool_use blocks from a Claude stream log (Edit/Write/Bash/Agent), or
notable tool-part records from an opencode log (OC-5), or notable tool-execution
records from a pi log (T-58), and records each as an ``ImplStepRecorded`` event
against the ticket log. Idempotent: ``step_id = f"step-{key}-{session_id}-{tool_id}"``
prevents re-appending on replay.

Also the one place that classifies a session's terminal outcome (``classify_result``,
``session_outcome``) — every render site (``cli.py``, ``tui/events.py``) and
``sessions.list_sessions`` calls through here rather than re-deriving the rule.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from . import event_log, store
from . import events as E

# Tools we consider "notable" and want to surface in the timeline
_EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
_COMMAND_TOOLS = frozenset({"Bash"})
_SUBAGENT_TOOLS = frozenset({"Agent"})
_NOTABLE_TOOLS = _EDIT_TOOLS | _COMMAND_TOOLS | _SUBAGENT_TOOLS


def _kind(tool_name: str, inp: dict) -> str:
    if tool_name in _EDIT_TOOLS:
        return "edit"
    if tool_name in _SUBAGENT_TOOLS:
        return "subagent"
    if tool_name in _COMMAND_TOOLS:
        cmd = inp.get("command", "")
        if "gh pr" in cmd:
            return "pr"
        return "command"
    return "note"


def _summary(tool_name: str, inp: dict) -> str:
    if tool_name == "Bash":
        return (inp.get("description") or inp.get("command", ""))[:120]
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        return (inp.get("file_path") or inp.get("path", ""))[:120]
    if tool_name == "Agent":
        return (inp.get("description") or inp.get("prompt", ""))[:120]
    return tool_name


# ---------------------------------------------------------------------------
# OC-5: opencode's own log grammar -- verified vocabulary (see spec T-41 Notes):
# a "step_start"/"tool_use"/"text"/"step_finish" record per line, each carrying
# its payload under a nested "part" object (tool name at ``part.tool``, per-call
# id at ``part.callID``, terminal reason at ``step_finish``'s ``part.reason``,
# tokens under ``part.tokens``). Both the underscored spelling above and
# opencode's own internal hyphenated Part.type spelling ("step-start"/"tool"/
# "step-finish") are accepted, since the exact wire spelling of a third-party
# CLI's --format json output is not something this package controls.
# ---------------------------------------------------------------------------
OC_STEP_START_TYPES = frozenset({"step_start", "step-start"})
OC_TOOL_USE_TYPES = frozenset({"tool_use", "tool"})
OC_TEXT_TYPES = frozenset({"text"})
OC_STEP_FINISH_TYPES = frozenset({"step_finish", "step-finish"})

# opencode tool ids (lowercase) mapped onto the SAME kind vocabulary as Claude's
# _kind above -- OC-5's Notes: "map opencode tools onto the existing ImplStep
# vocabulary rather than inventing a parallel one".
_OC_EDIT_TOOLS = frozenset({"edit", "write", "patch"})
_OC_COMMAND_TOOLS = frozenset({"bash"})
_OC_SUBAGENT_TOOLS = frozenset({"task", "agent", "subtask"})


def oc_part(obj: dict) -> tuple[str | None, dict]:
    """Extract ``(type, part)`` from one opencode.jsonl record. Accepts both the
    documented ``{"type": ..., "part": {...}}`` wrapper and a bare part object
    (``obj`` itself carrying ``type``/``tool``/``callID``/... directly)."""
    t = obj.get("type")
    part = obj.get("part") if isinstance(obj.get("part"), dict) else obj
    return t, part


def _oc_kind(tool_name: str, inp: dict) -> str:
    name = tool_name.lower()
    if name in _OC_EDIT_TOOLS:
        return "edit"
    if name in _OC_SUBAGENT_TOOLS:
        return "subagent"
    if name in _OC_COMMAND_TOOLS:
        cmd = inp.get("command", "")
        if "gh pr" in cmd:
            return "pr"
        return "command"
    return "note"


def oc_summary(tool_name: str, part: dict) -> str:
    """Human-readable one-liner for an opencode tool-part -- prefers the tool's
    own ``state.title`` (opencode's own human-readable label, present once a
    call is running/completed), falls back to the raw input, falls back to the
    bare tool name so a summary is never blank."""
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    inp = state.get("input") if isinstance(state.get("input"), dict) else {}
    title = state.get("title")
    name = tool_name.lower()
    if name in _OC_COMMAND_TOOLS:
        text = inp.get("command") or title
    elif name in _OC_EDIT_TOOLS:
        text = inp.get("filePath") or inp.get("path") or title
    else:
        text = title
    return (text or tool_name)[:120]


def _opencode_session_key(path: Path) -> str:
    """The per-session discriminator for an opencode log: the log FILENAME's own
    session slug (``reconcile-<key>-<epoch>``, group 1 of
    ``sessions._SESSION_FILE_RE`` -- the same string ``sessions.list_sessions``
    reports as ``session_id``), never anything derived from the payload. Trap
    (spec Notes): opencode's own log carries no ``system`` record the way Claude's
    does, and whether ``part.callID`` is unique ACROSS sessions is UNVERIFIED --
    two different sessions could reuse the same callID, so the discriminator has
    to come from something that's unique per-session by construction: the epoch
    already embedded in the filename at spawn time."""
    from .sessions import _SESSION_FILE_RE

    m = _SESSION_FILE_RE.match(path.name)
    return m.group(1) if m else path.stem


def _iter_opencode_steps(
    path: Path, session_key: str
) -> Iterator[tuple[str, str, int, str, str, str, str]]:
    """Yield ``(session_id, tool_id, turn, role, kind, tool_name, summary)`` for
    notable tool-part records in an opencode ``.opencode.jsonl`` log. ``turn``
    increments on each ``step_start`` record (opencode's own turn boundary)."""
    turn = 0
    for _offset, obj in iter_records(path):
        t, part = oc_part(obj)
        if t in OC_STEP_START_TYPES:
            turn += 1
            continue
        if t not in OC_TOOL_USE_TYPES:
            continue
        tool_name = part.get("tool", "")
        tool_id = part.get("callID", "")
        if not tool_name or not tool_id:
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        inp = state.get("input") if isinstance(state.get("input"), dict) else {}
        yield (
            session_key,
            tool_id,
            turn,
            "assistant",
            _oc_kind(tool_name, inp),
            tool_name,
            oc_summary(tool_name, part),
        )


# ---------------------------------------------------------------------------
# T-58: the `pi` coding-agent CLI's own log grammar (`pi --mode json`) --
# verified against a REAL captured stream (not hand-authored -- see spec Notes
# and tests/fixtures/*.pi.jsonl), matching the documented `AgentSessionEvent`/
# `AgentEvent` union (`docs/json.md` in `@earendil-works/pi-coding-agent`). One
# JSON object per line: a "session" header (carries a real per-invocation
# UUID at "id", the same role Claude's "system" record plays -- unlike
# opencode's log, which carries no session identity of its own), then
# "agent_start"/"turn_start"/"message_start"/"message_update"/"message_end"/
# "turn_end"/"agent_end" lifecycle events. A tool call is a
# "tool_execution_start" record (`toolCallId`, `toolName`, `args`) -- captured
# at call time, before its result, the same point Claude's own "assistant"
# tool_use block and opencode's "tool_use" record are captured at. The
# session's terminal outcome is NOT a separate record type the way Claude's
# "result" is: it's read off the last assistant message's `stopReason`
# ("stop" success, "error" a failure -- carrying `errorMessage`, e.g. the
# verified "pi returns 0 in json mode even on a 401" trap from spec Notes) once
# an "agent_end" record has actually been seen (no "agent_end" yet -- crashed
# mid-turn, or the writer just hasn't flushed it -- means "running", exactly
# like Claude/opencode's own not-yet-terminal case).
# ---------------------------------------------------------------------------
PI_SESSION_TYPE = "session"
PI_TOOL_START_TYPE = "tool_execution_start"
PI_TURN_START_TYPE = "turn_start"
PI_MESSAGE_END_TYPE = "message_end"
PI_AGENT_END_TYPE = "agent_end"

# Built-in pi tools (`pi --help`: "read, bash, edit, write, grep, find, ls") mapped
# onto the SAME kind vocabulary Claude's `_kind`/opencode's `_oc_kind` use, plus a
# defensive allowance for the `pi-subagents` extension's own tool name (not
# built-in, so its exact spelling is unverified here -- included the same
# defensive way opencode's `task`/`agent`/`subtask` are).
_PI_EDIT_TOOLS = frozenset({"edit", "write"})
_PI_COMMAND_TOOLS = frozenset({"bash"})
_PI_SUBAGENT_TOOLS = frozenset({"task", "agent", "subagent"})
_PI_NOTABLE_TOOLS = _PI_EDIT_TOOLS | _PI_COMMAND_TOOLS | _PI_SUBAGENT_TOOLS


def _pi_kind(tool_name: str, args: dict) -> str:
    name = tool_name.lower()
    if name in _PI_EDIT_TOOLS:
        return "edit"
    if name in _PI_SUBAGENT_TOOLS:
        return "subagent"
    if name in _PI_COMMAND_TOOLS:
        cmd = args.get("command", "")
        if "gh pr" in cmd:
            return "pr"
        return "command"
    return "note"


def pi_summary(tool_name: str, args: dict) -> str:
    """Human-readable one-liner for a pi tool call -- pi's own `edit` tool takes
    `path` (verified: not Claude's `file_path` or opencode's `filePath`)."""
    name = tool_name.lower()
    if name in _PI_COMMAND_TOOLS:
        text = args.get("command")
    elif name in _PI_EDIT_TOOLS:
        text = args.get("path") or args.get("file_path") or args.get("filePath")
    elif name in _PI_SUBAGENT_TOOLS:
        text = args.get("description") or args.get("prompt")
    else:
        text = None
    return (text or tool_name)[:120]


def _iter_pi_steps(
    path: Path,
) -> Iterator[tuple[str, str, int, str, str, str, str]]:
    """Yield ``(session_id, tool_id, turn, role, kind, tool_name, summary)`` for
    notable ``tool_execution_start`` records in a pi ``.pi.jsonl`` log. ``turn``
    increments on each ``turn_start`` record (pi's own turn boundary)."""
    session_id = ""
    turn = 0
    for _offset, obj in iter_records(path):
        t = obj.get("type")
        if t == PI_SESSION_TYPE and not session_id:
            session_id = obj.get("id", "")
        elif t == PI_TURN_START_TYPE:
            turn += 1
        elif t == PI_TOOL_START_TYPE:
            tool_name = obj.get("toolName", "")
            if tool_name.lower() not in _PI_NOTABLE_TOOLS:
                continue
            tool_id = obj.get("toolCallId", "")
            if not tool_id:
                continue
            args = obj.get("args") or {}
            yield (
                session_id,
                tool_id,
                turn,
                "assistant",
                _pi_kind(tool_name, args),
                tool_name,
                pi_summary(tool_name, args),
            )


def _pi_session_outcome(path: Path) -> dict:
    """Tail-scan a pi ``.pi.jsonl`` log for its terminal outcome (T-58). No
    "agent_end" record seen yet -> ``running`` (matches the truncated-final-line
    requirement: an incomplete last line is simply never yielded by
    :func:`iter_records`, so a process killed mid-write reads as still running,
    never as an error). Once "agent_end" is seen, the LAST assistant
    ``message_end``'s ``stopReason`` decides success/error -- verified trap
    (spec Notes): pi's own process exit code is 0 in json mode even on a 401,
    so the record stream, never the exit code, is what's classified here.
    pi's log carries no separate rate-limit signal the way Claude's
    ``rate_limit_event`` does (spec Notes / AC9) -- ``rate_limit_info`` is
    always ``None``. ``result`` carries ``num_turns`` (count of ``turn_start``
    records seen) so ``dispatcher.detect_zero_turn_spawns`` can read it exactly
    like Claude's ``result.num_turns``. ``provider`` (unlike Claude, always
    implicitly "anthropic") is pi's own last-seen assistant ``message.provider``
    -- e.g. "anthropic"/"ollama"/"google" -- read off the SAME real record
    (never guessed), so ``health.check_provider_availability`` (AC7) can resolve
    its confirmation-probe host per actual provider instead of assuming
    Anthropic for a runner that may be talking to any of several."""
    turns = 0
    saw_agent_end = False
    last_assistant_stop: str | None = None
    last_assistant_error: str | None = None
    last_assistant_provider: str | None = None
    for _offset, obj in iter_records(path):
        t = obj.get("type")
        if t == PI_TURN_START_TYPE:
            turns += 1
        elif t == PI_AGENT_END_TYPE:
            saw_agent_end = True
        elif t == PI_MESSAGE_END_TYPE:
            msg = obj.get("message") or {}
            if msg.get("role") == "assistant":
                last_assistant_stop = msg.get("stopReason")
                last_assistant_error = msg.get("errorMessage")
                last_assistant_provider = msg.get("provider")
    if not saw_agent_end:
        return {"outcome": "running", "result": None, "rate_limit_info": None,
                "provider": last_assistant_provider}
    errored = last_assistant_stop == "error"
    result = {
        "num_turns": turns,
        "stopReason": last_assistant_stop,
        "result": last_assistant_error if errored else "ok",
    }
    return {"outcome": "error" if errored else "success", "result": result,
            "rate_limit_info": None, "provider": last_assistant_provider}


def iter_records(path: Path, *, start: int = 0) -> Iterator[tuple[int, dict]]:
    """Yield ``(offset, record)`` for each complete JSON line in *path* from byte
    *start* onward. ``offset`` is the byte position immediately after the line,
    so callers (e.g. :mod:`maestro.ratelimit`) can persist it as a resume cursor.
    A trailing line with no newline yet (the writer is mid-append) is left
    unconsumed — the next call picks it up once it is complete.
    """
    with path.open("rb") as fh:
        fh.seek(start)
        pos = start
        for raw in fh:
            pos += len(raw)
            if not raw.endswith(b"\n"):
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield pos, obj


def _iter_steps(
    stream_path: Path,
) -> Iterator[tuple[str, str, int, str, str, str, str]]:
    """Yield ``(session_id, tool_id, turn, role, kind, tool_name, summary)`` for notable steps."""
    session_id = ""
    turn = 0

    for _offset, obj in iter_records(stream_path):
        t = obj.get("type")
        if t == "system" and not session_id:
            session_id = obj.get("session_id", "")
        elif t == "assistant":
            turn += 1
            msg = obj.get("message", {})
            role = msg.get("role", "assistant")
            for block in msg.get("content", []):
                if block.get("type") != "tool_use":
                    continue
                tool_name = block.get("name", "")
                if tool_name not in _NOTABLE_TOOLS:
                    continue
                tool_id = block.get("id", "")
                if not tool_id:
                    continue
                inp = block.get("input") or {}
                yield (
                    session_id,
                    tool_id,
                    turn,
                    role,
                    _kind(tool_name, inp),
                    tool_name,
                    _summary(tool_name, inp),
                )


def fold_stream(
    home: Path, key: str, stream_path: Path, *, actor: str = "reconciler"
) -> int:
    """Append one IMPL_STEP per notable tool_use in *stream_path*. Returns count
    appended. Dispatches on filename suffix: a Claude ``.stream.jsonl`` walks
    ``_iter_steps`` (session id from the log's own ``system`` record); an opencode
    ``.opencode.jsonl`` (OC-5) walks ``_iter_opencode_steps`` (session id from the
    filename, per ``_opencode_session_key``'s docstring); a pi ``.pi.jsonl``
    (T-58) walks ``_iter_pi_steps`` (session id from the log's own ``session``
    record, per that function's docstring)."""
    if stream_path.name.endswith(".opencode.jsonl"):
        records = _iter_opencode_steps(stream_path, _opencode_session_key(stream_path))
    elif stream_path.name.endswith(".pi.jsonl"):
        records = _iter_pi_steps(stream_path)
    else:
        records = _iter_steps(stream_path)
    appended = 0
    for session_id, tool_id, turn, role, kind, tool_name, summary in records:
        sid = f"step-{key}-{session_id}-{tool_id}"
        ev = event_log.append(
            home,
            key,
            E.IMPL_STEP,
            {"turn": turn, "role": role, "kind": kind, "tool": tool_name, "summary": summary},
            actor=actor,
            step_id=sid,
        )
        if ev is not None:
            appended += 1
    return appended


def format_resets_at(epoch) -> str:
    """Render a ``rate_limit_info.resetsAt`` epoch as a human-readable UTC timestamp."""
    if epoch is None:
        return "unknown"
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return "unknown"


def classify_result(obj: dict) -> dict:
    """Classify a stream-json ``result`` object's outcome.

    Outcome is derived from ``is_error`` / ``api_error_status`` — NOT ``subtype`` — since
    a 429 rejection reports ``subtype: "success"`` with ``is_error: true``. Returns
    ``{"outcome": "success"|"error"|"rate_limited", "is_error", "api_error_status",
    "subtype", "message"}``.
    """
    is_error = bool(obj.get("is_error"))
    api_error_status = obj.get("api_error_status")
    subtype = obj.get("subtype")
    errored = is_error or api_error_status is not None
    if errored:
        outcome = "rate_limited" if api_error_status == 429 else "error"
    elif subtype == "success":
        outcome = "success"
    else:
        outcome = "error"
    return {
        "outcome": outcome,
        "is_error": is_error,
        "api_error_status": api_error_status,
        "subtype": subtype,
        "message": obj.get("result"),
    }


def _opencode_session_outcome(path: Path) -> dict:
    """Tail-scan an opencode ``.opencode.jsonl`` log for its terminal
    ``step_finish`` record (OC-5). ``reason`` other than the clean-stop value
    reports ``error`` -- opencode's own log carries no separate rate-limit
    signal the way Claude's ``rate_limit_event`` does, and ``cost: 0`` here is
    genuinely a local model's true cost, never a reason to report
    ``unavailable`` (``spend.py``'s ``over_ceiling`` fails OPEN on that, which
    would disarm the ceiling -- see spec Notes)."""
    reason: str | None = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t, part = oc_part(obj)
            if t in OC_STEP_FINISH_TYPES:
                reason = part.get("reason")
    if reason is None:
        return {"outcome": "running", "result": None, "rate_limit_info": None}
    outcome = "error" if reason == "error" else "success"
    return {"outcome": outcome, "result": {"reason": reason}, "rate_limit_info": None}


def session_outcome(stream_path: Path) -> dict:
    """Tail-scan *stream_path* for its terminal ``result`` and any ``rate_limit_event``.

    Returns ``{"outcome": ..., "result": <result obj or None>, "rate_limit_info": ...}``.
    ``outcome`` is one of ``success`` / ``error`` / ``rate_limited`` (terminal result seen),
    ``running`` (no terminal record yet), or ``unknown`` (none of ``.stream.jsonl``,
    ``.opencode.jsonl``, or ``.pi.jsonl`` — e.g. a plain-text session). An opencode
    ``.opencode.jsonl`` log (OC-5) is dispatched to ``_opencode_session_outcome``, and a
    pi ``.pi.jsonl`` log (T-58) to ``_pi_session_outcome``, instead of the Claude-shaped
    parse below. A rejected ``rate_limit_event`` only escalates an already-errored result
    to ``rate_limited``; it never overrides an otherwise-``success`` result — the event is
    context, not the verdict. A pi log's dict also carries ``provider`` (T-58,
    ``_pi_session_outcome``'s own docstring) -- ``None`` for a Claude/opencode log,
    where the provider is either implicit (Claude) or not surfaced (opencode).
    """
    if stream_path.name.endswith(".opencode.jsonl"):
        return _opencode_session_outcome(stream_path)
    if stream_path.name.endswith(".pi.jsonl"):
        return _pi_session_outcome(stream_path)
    if not stream_path.name.endswith(".stream.jsonl"):
        return {"outcome": "unknown", "result": None, "rate_limit_info": None}

    result_obj: dict | None = None
    rate_limit_info: dict | None = None
    with stream_path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "result":
                result_obj = obj
            elif t == "rate_limit_event":
                rate_limit_info = obj.get("rate_limit_info")

    if result_obj is None:
        return {"outcome": "running", "result": None, "rate_limit_info": rate_limit_info}

    classified = classify_result(result_obj)
    outcome = classified["outcome"]
    if outcome == "error" and rate_limit_info and rate_limit_info.get("status") == "rejected":
        outcome = "rate_limited"
    return {"outcome": outcome, "result": result_obj, "rate_limit_info": rate_limit_info}


def fold_current_session(home: Path, key: str, *, actor: str = "reconciler") -> int:
    """Fold the stream log that the current reconciler claim points to (if any).

    Returns 0 if no claim or no stream log is found (safe no-op).
    """
    from . import claims

    claim = claims.read_claim(home, key)
    if not claim:
        return 0
    log_path = claim.get("log_path")
    if not log_path:
        return 0
    p = Path(log_path)
    if not p.exists() or not (p.name.endswith(".stream.jsonl") or p.name.endswith(".pi.jsonl")):
        return 0
    return fold_stream(home, key, p, actor=actor)
