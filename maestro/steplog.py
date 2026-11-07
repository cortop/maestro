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
import re
from pathlib import Path
from typing import Iterator

from . import claims, event_log, store
from . import events as E

# Tools we consider "notable" and want to surface in the timeline
_EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
_COMMAND_TOOLS = frozenset({"Bash"})
_SUBAGENT_TOOLS = frozenset({"Agent"})
_NOTABLE_TOOLS = _EDIT_TOOLS | _COMMAND_TOOLS | _SUBAGENT_TOOLS

# T-89 (Gap 3): a session log holding only non-JSON runner output (a stderr
# splat -- ``ClaudeSessions.spawn`` Popens stdout AND stderr into the same
# log file with no JSON framing of its own, e.g. a `claude` CLI network error
# printed before any stream-json record could be written) is bounded to this
# many trailing lines when classified as "crashed" below, so a huge garbage
# log can't bloat the outcome dict or whatever later stores it (a Failed
# payload, a rendered detail pane).
_CRASH_TAIL_LINES = 20


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
PI_TOOL_END_TYPE = "tool_execution_end"
PI_TURN_START_TYPE = "turn_start"
PI_MESSAGE_END_TYPE = "message_end"
PI_AGENT_END_TYPE = "agent_end"
PI_AUTO_RETRY_START_TYPE = "auto_retry_start"
PI_AGENT_SETTLED_TYPE = "agent_settled"
PI_TOOL_UPDATE_TYPE = "tool_execution_update"
# Raw line prefixes of the high-volume pi records no reader needs; a skip-list, so a
# key reorder in pi only makes parsing slower, never wrong.
PI_NOISE_PREFIXES = (b'{"type":"message_update"', b'{"type":"tool_execution_update"')

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
    elif name == "read":
        text = args.get("path") or args.get("file_path") or args.get("filePath")
    elif name in ("grep", "find"):
        pattern = args.get("pattern") or args.get("query") or args.get("name")
        where = args.get("path")
        text = " ".join(str(x) for x in (pattern, where) if x) or None
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
    for _offset, obj in iter_records(path, skip_prefixes=PI_NOISE_PREFIXES):
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


STEP_PREFIX = "Step:"
STEP_MAX_CHARS = 80


def step_lines(text: str) -> list[str]:
    """Declared ``Step:`` lines in ``text``, in order. A line counts only when it
    starts with ``Step:``; the label is trimmed, capped at ``STEP_MAX_CHARS``, and an
    empty one is skipped."""
    out: list[str] = []
    for line in (text or "").splitlines():
        if not line.startswith(STEP_PREFIX):
            continue
        label = line[len(STEP_PREFIX):].strip()
        if label:
            out.append(label[:STEP_MAX_CHARS].rstrip())
    return out


def record_steps(obj: dict) -> list[str]:
    """``step_lines`` over the assistant text of one session-log record: a Claude
    ``assistant`` record's text blocks or a pi assistant ``message_end``'s text blocks."""
    if obj.get("type") == PI_MESSAGE_END_TYPE:
        msg = obj.get("message") or {}
    elif obj.get("type") == "assistant":
        msg = obj.get("message") or {}
    else:
        return []
    if msg.get("role") != "assistant":
        return []
    content = msg.get("content")
    if isinstance(content, str):
        return step_lines(content)
    out: list[str] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            out.extend(step_lines(block.get("text") or ""))
    return out


_NARRATION_LEAD = re.compile(r"^(let me|let's|now,?|i'll|i will|i'm going to)\s+", re.IGNORECASE)
_CLAUSE_MAX_CHARS = 80
QUIET_AFTER_S = 60
_LIVE_CACHE: dict[str, tuple[tuple[int, int], dict, dict]] = {}


def narration_clause(text: str) -> str:
    """The final clause of the last non-``Step:`` line of *text*: lead-ins such as "Let me"
    or "Now" and a trailing colon are stripped; empty when nothing is left."""
    lines = [ln.strip() for ln in (text or "").splitlines()
             if ln.strip() and not ln.startswith(STEP_PREFIX)]
    if not lines:
        return ""
    sentence = re.split(r"(?<=[.!?])\s+", lines[-1])[-1].strip().rstrip(":.!").strip()
    while True:
        stripped = _NARRATION_LEAD.sub("", sentence)
        if stripped == sentence:
            break
        sentence = stripped
    return sentence[:_CLAUSE_MAX_CHARS].rstrip()


def _record_epoch(obj: dict) -> float | None:
    raw = obj.get("timestamp")
    return store.iso_to_epoch(raw) if isinstance(raw, str) else None


def _assistant_text_and_tools(obj: dict) -> tuple[str, list[tuple[str, str, str]]]:
    """(text, [(tool_id, tool_name, summary)]) of one assistant record in either log shape."""
    t = obj.get("type")
    if t not in ("assistant", PI_MESSAGE_END_TYPE):
        return "", []
    msg = obj.get("message") or {}
    if msg.get("role") != "assistant":
        return "", []
    content = msg.get("content")
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    tools: list[tuple[str, str, str]] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(block.get("text") or "")
        elif t == "assistant" and block.get("type") == "tool_use" and block.get("id"):
            name = block.get("name", "")
            tools.append((block["id"], name, _summary(name, block.get("input") or {})))
    return "\n".join(texts), tools


def live_status(path: Path, state: dict | None = None) -> tuple[dict, dict]:
    """Incremental live read of a running session log -> ``(status, state)``.

    *state* (``None`` on the first call) carries the byte offset and what has been seen
    so far, so a later call parses only appended bytes; an unchanged ``(size, mtime)``
    returns the memoized status without opening the file. ``status`` keys: ``step`` (latest
    ``Step:`` label), ``clause`` (last narration clause), ``tool`` (``{"name", "summary",
    "started"}`` for the call still in flight, else ``None``) and ``last_ts`` (epoch of the
    newest record, falling back to the file mtime). Handles Claude and pi logs; a missing
    file yields an empty status.
    """
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return {"step": "", "clause": "", "tool": None, "last_ts": None}, state or {}
    sig = (st.st_size, st.st_mtime_ns)
    cached = _LIVE_CACHE.get(key)
    if state and cached and cached[0] == sig and state.get("offset") == st.st_size:
        return dict(cached[1]), state
    if not state or state.get("offset", 0) > st.st_size:
        state = {"offset": 0, "step": "", "clause": "", "tools": {}, "last_ts": None}
    tools: dict = state["tools"]
    offset = state["offset"]
    for offset, obj in iter_records(path, start=state["offset"], skip_prefixes=PI_NOISE_PREFIXES):
        ts = _record_epoch(obj) or st.st_mtime
        state["last_ts"] = ts
        t = obj.get("type")
        text, started = _assistant_text_and_tools(obj)
        steps = step_lines(text)
        if steps:
            state["step"] = steps[-1]
        clause = narration_clause(text)
        if clause:
            state["clause"] = clause
        for tool_id, name, summary in started:
            tools[tool_id] = {"name": name, "summary": summary, "started": ts}
        if t == PI_TOOL_START_TYPE and obj.get("toolCallId"):
            name = obj.get("toolName", "")
            tools[obj["toolCallId"]] = {"name": name, "started": ts,
                                        "summary": pi_summary(name, obj.get("args") or {})}
        elif t == "tool_execution_end":
            tools.pop(obj.get("toolCallId"), None)
        elif t == "user":
            for block in (obj.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tools.pop(block.get("tool_use_id"), None)
    state["offset"] = offset
    if state["last_ts"] is None:
        state["last_ts"] = st.st_mtime
    flight = max(tools.values(), key=lambda v: v["started"], default=None)
    status = {"step": state["step"], "clause": state["clause"],
              "tool": dict(flight) if flight else None, "last_ts": state["last_ts"]}
    _LIVE_CACHE[key] = (sig, status, state)
    return dict(status), state


def live_texts(status: dict, now: float) -> tuple[str, str]:
    """``(doing, now_text)`` for a live status: doing is the Step label or, failing that,
    the narration clause; now is the in-flight tool with elapsed time, else ``quiet Nm``
    once the newest record is old, else the narration clause."""
    tool = status.get("tool")
    last = status.get("last_ts")
    if tool:
        label = tool.get("summary") or tool.get("name") or "tool"
        now_text = f"{label} · {_elapsed(now - tool['started'])}"
    elif last is not None and now - last >= QUIET_AFTER_S:
        now_text = f"quiet {int((now - last) // 60)}m"
    else:
        now_text = status.get("clause") or ""
    return status.get("step") or status.get("clause") or now_text, now_text


def _elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


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
    for _offset, obj in iter_records(path, skip_prefixes=PI_NOISE_PREFIXES):
        t = obj.get("type")
        if t == PI_TURN_START_TYPE:
            turns += 1
        elif t == PI_AGENT_END_TYPE or t == PI_AGENT_SETTLED_TYPE:
            saw_agent_end = True
        elif t == PI_AUTO_RETRY_START_TYPE:
            # the preceding agent_end was not final: the retry decides the outcome
            saw_agent_end = False
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


def iter_records(
    path: Path, *, start: int = 0, skip_prefixes: tuple[bytes, ...] = ()
) -> Iterator[tuple[int, dict]]:
    """Yield ``(offset, record)`` for each complete JSON line in *path* from byte
    *start* onward. ``offset`` is the byte position immediately after the line,
    so callers (e.g. :mod:`maestro.ratelimit`) can persist it as a resume cursor.
    A trailing line with no newline yet (the writer is mid-append) is left
    unconsumed — the next call picks it up once it is complete. A complete line that
    starts with any of *skip_prefixes* (raw bytes) is dropped without being decoded;
    offsets still advance over it.
    """
    with path.open("rb") as fh:
        fh.seek(start)
        pos = start
        for raw in fh:
            pos += len(raw)
            if not raw.endswith(b"\n"):
                break
            if skip_prefixes and raw.startswith(skip_prefixes):
                continue
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


# Closed vocabulary of `outcome_class` labels; unmatched error text is
# `error:unclassified`, never an infra label.
OUTCOME_CLASSES = (
    "success", "rate_limited", "running", "crashed", "unknown",
    "error:econnrefused", "error:enotfound", "error:timeout",
    "error:auth", "error:server", "error:unclassified",
)

# (needle in lower-cased text, label): first match wins.
_ERROR_TEXT_RULES = (
    ("econnrefused", "error:econnrefused"),
    ("enotfound", "error:enotfound"),
    ("etimedout", "error:timeout"),
    ("timed out", "error:timeout"),
    ("timeout", "error:timeout"),
    ("unauthorized", "error:auth"),
    ("authentication", "error:auth"),
    ("invalid api key", "error:auth"),
    ("overloaded", "error:server"),
    ("internal server error", "error:server"),
)


def outcome_class(outcome: dict) -> str:
    """Map a ``session_outcome`` dict (Claude or pi log) to one `OUTCOME_CLASSES` label.

    Pure: reads only the dict. A non-error outcome keeps its own name; an error is
    labelled from its ``api_error_status`` (401/403 auth, 5xx server) or, failing that,
    the error text; anything unmatched is ``error:unclassified``.
    """
    name = outcome.get("outcome")
    if name in ("success", "rate_limited", "running", "crashed", "unknown"):
        return name
    if name != "error":
        return "error:unclassified"
    result = outcome.get("result") or {}
    status = result.get("api_error_status")
    if isinstance(status, int):
        if status in (401, 403):
            return "error:auth"
        if status >= 500:
            return "error:server"
    text = result.get("result") or result.get("tail") or ""
    if not isinstance(text, str):
        text = str(text)
    text = text.lower()
    for needle, label in _ERROR_TEXT_RULES:
        if needle in text:
            return label
    return "error:unclassified"


def terminal_reasons(home: Path, key: str, count: int) -> str:
    """Summarise the newest *count* terminal sessions of *key* as e.g. ``ECONNREFUSED x5``.

    Sessions still running are skipped; returns ``""`` when none are terminal.
    """
    from . import sessions
    tally: dict[str, int] = {}
    for sess in sessions.list_sessions(home, key)[:max(0, count)]:
        label = outcome_class(session_outcome(Path(sess["path"])))
        if label == "running":
            continue
        label = label.rsplit(":", 1)[-1].upper()
        tally[label] = tally.get(label, 0) + 1
    return ", ".join(f"{label} x{n}" for label, n in tally.items())


def result_summary(records) -> dict:
    """Fold Claude stream-json *records* into the console header's numbers.

    ``cost`` / ``turns`` / ``duration_ms`` / ``denials`` come from the LAST ``result``
    (``total_cost_usd`` is a running total -- never summed); each is ``None`` when absent.
    ``outcome`` is ``running`` until a ``result`` lands, then ``classify_result``'s verdict
    (an errored result escalates to ``rate_limited`` on a rejected ``rate_limit_event``, as
    ``session_outcome`` does). ``tokens`` is input+output over the last ``usage`` seen per
    assistant ``message.id`` (Claude repeats it on every content-block record).
    """
    result: dict | None = None
    rate_limit_info: dict | None = None
    usage_by_msg: dict = {}
    anon = 0
    for obj in records:
        if not isinstance(obj, dict):
            continue
        t = obj.get("type")
        if t == "result":
            result = obj
        elif t == "rate_limit_event":
            rate_limit_info = obj.get("rate_limit_info")
        elif t == "assistant":
            msg = obj.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            mid = msg.get("id")
            if mid is None:
                anon += 1
                mid = f"_anon{anon}"
            usage_by_msg[mid] = msg["usage"]
    tokens = sum(int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
                 for u in usage_by_msg.values())
    out = {"cost": None, "turns": None, "duration_ms": None, "denials": None,
           "outcome": "running", "tokens": tokens}
    if result is not None:
        outcome = classify_result(result)["outcome"]
        if outcome == "error" and rate_limit_info and rate_limit_info.get("status") == "rejected":
            outcome = "rate_limited"
        denials = result.get("permission_denials")
        out.update(cost=result.get("total_cost_usd"), turns=result.get("num_turns"),
                   duration_ms=result.get("duration_ms"), outcome=outcome,
                   denials=len(denials) if isinstance(denials, list) else None)
    return out


def _opencode_session_outcome(path: Path) -> dict:
    """Tail-scan an opencode ``.opencode.jsonl`` log for its terminal
    ``step_finish`` record (OC-5). ``reason`` other than the clean-stop value
    reports ``error`` -- opencode's own log carries no separate rate-limit
    signal the way Claude's ``rate_limit_event`` does, and ``cost: 0`` here is
    genuinely a local model's true cost, never a reason to report
    ``unavailable`` (``spend.py``'s ``over_ceiling`` fails OPEN on that, which
    would disarm the ceiling -- see spec Notes)."""
    reason: str | None = None
    for obj in store.iter_jsonl(path, errors="replace"):
        t, part = oc_part(obj)
        if t in OC_STEP_FINISH_TYPES:
            reason = part.get("reason")
    if reason is None:
        return {"outcome": "running", "result": None, "rate_limit_info": None}
    outcome = "error" if reason == "error" else "success"
    return {"outcome": outcome, "result": {"reason": reason}, "rate_limit_info": None}


_RUNNER_BY_SUFFIX = ((".stream.jsonl", "claude"), (".opencode.jsonl", "opencode"), (".pi.jsonl", "pi"))


def _model_str(v) -> str | None:
    """A model id as a non-empty string; opencode may nest it as ``{providerID, modelID}``."""
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        return _model_str(v.get("modelID") or v.get("id") or v.get("model"))
    return None


def _record_model(obj: dict) -> str | None:
    """The model a single log record names, across the claude / opencode / pi shapes."""
    if obj.get("type") == "system" and obj.get("subtype") == "init":
        m = _model_str(obj.get("model"))
        if m:
            return m
    part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
    for holder in (obj.get("message"), part, obj.get("info"), obj):
        if isinstance(holder, dict):
            m = _model_str(holder.get("model")) or _model_str(holder.get("modelID"))
            if m:
                return m
    return None


def format_session_header(sess: dict, outcome: str, info: dict) -> str:
    """The one-line per-session banner shared by ``maestro logs`` and the TUI (T-119/T-120)."""
    return (f"=== session {sess['session_id']} | {sess['ts']} | {sess['format']} | {outcome} | "
            f"runner: {info['runner']} | model: {info['model']} ===")


def session_model(path: Path) -> dict:
    """What actually ran a session, read from its log: ``{"model": ..., "runner": ...}``.

    ``runner`` follows the log's format (``claude`` / ``opencode`` / ``pi``; ``unknown`` for
    plain text). ``model`` is a Claude log's ``system``/``init`` model, falling back to the
    first record carrying one (an assistant ``message.model``); opencode/pi likewise. A
    log with no model information, an unreadable one, or a malformed/truncated line yields
    ``unknown`` -- this never raises.
    """
    name = path.name
    runner = next((r for suffix, r in _RUNNER_BY_SUFFIX if name.endswith(suffix)), "unknown")
    model = None
    if runner != "unknown":
        try:
            skip = PI_NOISE_PREFIXES if runner == "pi" else ()
            for _offset, obj in iter_records(path, skip_prefixes=skip):
                model = _record_model(obj) if isinstance(obj, dict) else None
                if model:
                    break
        except (OSError, ValueError):
            model = None
    return {"model": model or "unknown", "runner": runner}


def session_outcome(stream_path: Path, *, pid: int | None = None) -> dict:
    """Tail-scan *stream_path* for its terminal ``result`` and any ``rate_limit_event``.

    Returns ``{"outcome": ..., "result": <result obj or None>, "rate_limit_info": ...}``.
    ``outcome`` is one of ``success`` / ``error`` / ``rate_limited`` (terminal result seen),
    ``crashed`` (T-89, Claude logs only -- see below), ``running`` (no terminal record yet),
    or ``unknown`` (none of ``.stream.jsonl``, ``.opencode.jsonl``, or ``.pi.jsonl`` — e.g. a
    plain-text session). An opencode ``.opencode.jsonl`` log (OC-5) is dispatched to
    ``_opencode_session_outcome``, and a pi ``.pi.jsonl`` log (T-58) to
    ``_pi_session_outcome``, instead of the Claude-shaped parse below. A rejected
    ``rate_limit_event`` only escalates an already-errored result to ``rate_limited``; it
    never overrides an otherwise-``success`` result — the event is context, not the verdict.
    A pi log's dict also carries ``provider`` (T-58, ``_pi_session_outcome``'s own
    docstring) -- ``None`` for a Claude/opencode log, where the provider is either implicit
    (Claude) or not surfaced (opencode).

    ``pid`` (T-89, Gap 3) is the OPTIONAL known pid of the process that wrote this Claude
    stream log -- passed only by a caller that already has one (e.g. a claim record). When
    the log holds SOME raw output but not one single parseable JSON line (a stderr splat --
    ``ClaudeSessions.spawn`` Popens stdout+stderr into the same file with no framing of its
    own, so a `claude` CLI error printed before it could write its first stream-json record
    looks identical to "still running" otherwise) and *pid* is given and no longer alive,
    this reports ``crashed`` instead of ``running`` -- the process is provably gone, so
    "running" is simply wrong, not just incomplete. ``result`` for that outcome is
    ``{"tail": <last _CRASH_TAIL_LINES lines of the log>}``. Without a *pid* (the default),
    behavior is unchanged -- a log with no parseable result always reads as ``running``,
    exactly as before.
    """
    if stream_path.name.endswith(".opencode.jsonl"):
        return _opencode_session_outcome(stream_path)
    if stream_path.name.endswith(".pi.jsonl"):
        return _pi_session_outcome(stream_path)
    if not stream_path.name.endswith(".stream.jsonl"):
        return {"outcome": "unknown", "result": None, "rate_limit_info": None}

    result_obj: dict | None = None
    rate_limit_info: dict | None = None
    saw_json = False
    saw_raw = False
    tail: list[str] = []
    with stream_path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            saw_raw = True
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                tail.append(raw)
                del tail[:-_CRASH_TAIL_LINES]
                continue
            saw_json = True
            t = obj.get("type")
            if t == "result":
                result_obj = obj
            elif t == "rate_limit_event":
                rate_limit_info = obj.get("rate_limit_info")

    if result_obj is None:
        if (not saw_json and saw_raw and pid is not None
                and not claims.pid_alive(pid)):
            return {"outcome": "crashed", "result": {"tail": "\n".join(tail)},
                    "rate_limit_info": rate_limit_info}
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
