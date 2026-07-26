"""Step-log folder — reads a stream.jsonl session log and appends IMPL_STEP events.

Extracts notable tool_use blocks from a Claude stream log (Edit/Write/Bash/Agent) and
records each as an ``ImplStepRecorded`` event against the ticket log.  Idempotent:
``step_id = f"step-{key}-{session_id}-{tool_id}"`` prevents re-appending on replay.
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
    """Append one IMPL_STEP per notable tool_use in *stream_path*. Returns count appended."""
    appended = 0
    for session_id, tool_id, turn, role, kind, tool_name, summary in _iter_steps(stream_path):
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
    if not p.exists() or not p.name.endswith(".stream.jsonl"):
        return 0
    return fold_stream(home, key, p, actor=actor)
