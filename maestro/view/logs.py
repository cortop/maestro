"""Logs view-model: a ticket's sessions split into the live one (full detail) and past ones (summary rows)."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .. import claims, event_log, steplog, store
from ..sessions import list_pruned, list_sessions
from . import rounds as rounds_view


@dataclass(frozen=True)
class PastSession:
    session_id: str
    summary: str          # one-line digest: start, runner format, outcome, model, calls
    sess: dict            # the ``list_sessions`` / ``list_pruned`` entry
    pruned: bool = False  # raw log gone; ``summary`` comes from the persisted sidecar


@dataclass(frozen=True)
class TicketLogs:
    key: str
    live: dict | None                 # the session shown with full step detail, None when none exist
    past: tuple[PastSession, ...]     # newest first; empty means no summary section at all


def live_path(home: Path, key: str, sessions: list[dict]) -> str | None:
    """The session log being tailed: the claim's log, else the newest captured session."""
    claim = claims.read_claim(home, key)
    if claim and claim.get("log_path"):
        return str(claim["log_path"])
    return sessions[0]["path"] if sessions else None


def build(home: Path, key: str) -> TicketLogs:
    """Every session of *key* with the live one separated from collapsed past summaries."""
    listed = list_sessions(home, key, with_outcome=True)
    live = live_path(home, key, listed)
    events = event_log.read(home, key) if (listed or list_pruned(home, key)) else []
    newest = listed[0]["path"] if listed else None
    entries = [*listed, *list_pruned(home, key)]
    entries.sort(key=lambda s: s.get("epoch", 0.0), reverse=True)
    live_sess, past = None, []
    for s in entries:
        if not s.get("pruned") and s["path"] == live:
            live_sess = s
            continue
        d = rounds_view.session_digest(home, key, s, events, newest)
        past.append(PastSession(s["session_id"], steplog.format_session_oneliner(s, d), s,
                                bool(s.get("pruned"))))
    return TicketLogs(key, live_sess, tuple(past))


def detail_lines(home: Path, key: str, past: PastSession) -> list[str]:
    """The expanded view of a past session: its step digest lines."""
    d = rounds_view.session_digest(home, key, past.sess)
    return steplog.format_session_digest(d)


# --- session transcript: the steps `maestro logs` prints, paged ---------------------------------

PAGE_STEPS = 100  # steps one response carries; a log is never rendered whole


def result_line(obj: dict) -> str:
    classified = steplog.classify_result(obj)
    dur = obj.get("duration_ms")
    suffix = f" ({dur}ms)" if dur else ""
    outcome = classified["outcome"]
    if outcome == "success":
        return f"[result:{classified['subtype']}]{suffix}"
    parts = [f"[result:{outcome}]"]
    if classified["api_error_status"] is not None:
        parts.append(f"api_error_status={classified['api_error_status']}")
    if classified["message"]:
        parts.append(classified["message"])
    return " ".join(parts) + suffix


def rate_limit_line(obj: dict) -> str:
    info = obj.get("rate_limit_info") or {}
    resets_at = steplog.format_resets_at(info.get("resetsAt"))
    return f"[rate_limit:{info.get('rateLimitType', '')}] status={info.get('status', '')} resetsAt={resets_at}"


def assistant_steps(obj: dict) -> Iterator[str]:
    for block in obj["message"].get("content", []):
        btype = block.get("type")
        if btype == "text":
            yield block["text"]
        elif btype == "tool_use":
            yield f"[tool_use:{block['name']}] {json.dumps(block.get('input', {}))[:120]}"


def stream_steps(path: Path) -> Iterator[str]:
    """Steps of a stream-json log: assistant messages flush in order ahead of each result."""
    pending: dict[str, dict] = {}
    for obj in store.iter_jsonl(path):
        if obj.get("type") == "assistant":
            pending[obj["message"]["id"]] = obj
        elif obj.get("type") == "rate_limit_event":
            yield rate_limit_line(obj)
        elif obj.get("type") == "result":
            for msg in pending.values():
                yield from assistant_steps(msg)
            pending.clear()
            yield result_line(obj)
    for msg in pending.values():
        yield from assistant_steps(msg)


def opencode_record_steps(obj: dict) -> Iterator[str]:
    """Steps of one opencode record; an unrecognised one still shows its bare ``text``."""
    t, part = steplog.oc_part(obj)
    if t in steplog.OC_TEXT_TYPES:
        if part.get("text"):
            yield part["text"]
    elif t in steplog.OC_TOOL_USE_TYPES:
        tool_name = part.get("tool", "?")
        yield f"[tool_use:{tool_name}] {steplog.oc_summary(tool_name, part)}"
    elif t in steplog.OC_STEP_FINISH_TYPES:
        if part.get("reason"):
            yield f"[step_finish] reason={part['reason']}"
    elif t not in steplog.OC_STEP_START_TYPES:
        text = obj.get("text")
        if isinstance(text, str) and text:
            yield text


def opencode_steps(path: Path) -> Iterator[str]:
    """Steps of an opencode.jsonl log."""
    for obj in store.iter_jsonl(path, errors="replace"):
        yield from opencode_record_steps(obj)


def pi_record_steps(obj: dict) -> Iterator[str]:
    """Steps of one pi record; tool updates and deltas yield nothing."""
    t = obj.get("type")
    if t == steplog.PI_TOOL_START_TYPE:
        tool_name = obj.get("toolName", "?")
        yield f"[tool:{tool_name}] {steplog.pi_summary(tool_name, obj.get('args') or {})}"
    elif t == steplog.PI_MESSAGE_END_TYPE:
        msg = obj.get("message") or {}
        if msg.get("role") != "assistant":
            return
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    yield text
        if msg.get("stopReason") == "error":
            yield f"[error] {msg.get('errorMessage') or 'error'}"
    elif t == steplog.PI_AUTO_RETRY_START_TYPE:
        yield "[auto_retry]"
    elif t == steplog.PI_TURN_START_TYPE:
        yield "[turn_start]"


def pi_steps(path: Path) -> Iterator[str]:
    """Steps of a pi.jsonl log, skipping noise records unparsed."""
    for _offset, obj in steplog.iter_records(path, skip_prefixes=steplog.PI_NOISE_PREFIXES):
        yield from pi_record_steps(obj)


def text_steps(path: Path) -> Iterator[str]:
    """A plain-text log, one step per line."""
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            yield line.rstrip("\n")


def session_steps(sess: dict) -> Iterator[str]:
    """The steps `maestro logs` prints for *sess*, through its own format's reader."""
    reader = {"stream-json": stream_steps, "opencode": opencode_steps, "pi": pi_steps}.get(
        sess["format"], text_steps)
    return reader(Path(sess["path"]))


def is_error_step(step: str) -> bool:
    """True for a failed result, an error line or a rate-limit notice."""
    if step.startswith("[result:"):
        return not step.startswith("[result:success]")
    return step.startswith(("[error]", "[rate_limit:"))


@dataclass(frozen=True)
class Step:
    index: int       # 0-based position in the whole transcript
    text: str
    error: bool


@dataclass(frozen=True)
class StepPage:
    session_id: str
    steps: tuple[Step, ...]   # chronological
    total: int                # steps in the whole transcript right now
    earlier: int              # steps before the first one shown; 0 when none


_CACHE: dict[str, tuple[tuple[int, int], list[str]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 8


def _all_steps(sess: dict) -> list[str]:
    """Every step of *sess*, memoized by the log's size and mtime so paging re-reads nothing."""
    path = sess["path"]
    st = Path(path).stat()
    stamp = (st.st_size, st.st_mtime_ns)
    with _CACHE_LOCK:
        hit = _CACHE.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
    steps = list(session_steps(sess))
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[path] = (stamp, steps)
    return steps


def find_session(home: Path, key: str, session_id: str) -> dict | None:
    """The captured (not pruned) session named *session_id*, or None."""
    return next((s for s in list_sessions(home, key) if s["session_id"] == session_id), None)


def find_any_session(home: Path, key: str, session_id: str) -> dict | None:
    """A session by id, captured or pruned."""
    return find_session(home, key, session_id) or next(
        (s for s in list_pruned(home, key) if s["session_id"] == session_id), None)


def step_page(sess: dict, *, before: int | None = None, after: int | None = None,
              limit: int = PAGE_STEPS) -> StepPage:
    """A window of *sess*'s steps: the newest *limit* by default, the *limit* ending just
    before index *before* (earlier steps), or everything from index *after* on (new steps)."""
    try:
        steps = _all_steps(sess)
    except OSError:
        steps = []
    total = len(steps)
    if after is not None:
        lo, hi = min(max(after, 0), total), total
    else:
        hi = total if before is None else min(max(before, 0), total)
        lo = max(0, hi - limit)
    shown = tuple(Step(i, steps[i], is_error_step(steps[i])) for i in range(lo, hi))
    return StepPage(sess["session_id"], shown, total, lo)
