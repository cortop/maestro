"""Event-timeline rendering — no textual dependency, importable in tests."""
from __future__ import annotations

import json as _json
import os
from pathlib import Path

from .. import event_log, events as ev_types, store
from ..steplog import (OC_STEP_FINISH_TYPES, OC_STEP_START_TYPES,
                        OC_TEXT_TYPES, OC_TOOL_USE_TYPES, PI_AGENT_END_TYPE,
                        PI_MESSAGE_END_TYPE, PI_TOOL_START_TYPE,
                        classify_result, format_resets_at, oc_part,
                        oc_summary, pi_summary)

_EM = "—"
_TAIL_N = 20

_IMPL_STEP = "ImplStepRecorded"

_KIND_BADGE = {
    "edit":     "[green]edit[/green]",
    "command":  "[yellow]cmd[/yellow]",
    "subagent": "[cyan]agent[/cyan]",
    "pr":       "[magenta]pr[/magenta]",
    "note":     "[dim]note[/dim]",
}

_MILESTONE_COLOR = {
    "PhaseChanged":     "bold blue",
    "PrOpened":         "bold magenta",
    "PrUpdated":        "bold magenta",
    "CiObserved":       "bold yellow",
    "Finalized":        "bold green",
    "Failed":           "bold red",
    "Stalled":          "bold red",
    "QuestionAsked":    "yellow",
    "QuestionAnswered": "green",
}


def render_event(ev: dict) -> str:
    """Format one event as a Rich markup line: seq ts type actor payload-summary."""
    ts = (ev.get("ts") or "")[:19]
    seq = ev.get("seq", "?")
    type_ = ev.get("type", _EM)
    actor = ev.get("actor", _EM)
    payload = ev.get("payload") or {}

    if type_ == _IMPL_STEP:
        kind = payload.get("kind", "note")
        summary = payload.get("summary", "")[:80]
        badge = _KIND_BADGE.get(kind, f"[dim]{kind}[/dim]")
        return (
            f"[dim]{seq:>4}[/dim] [cyan]{ts}[/cyan] "
            f"  {badge} [dim]{summary}[/dim]"
        )

    if payload:
        summary = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:3])
    else:
        summary = ""

    color = _MILESTONE_COLOR.get(type_)
    type_markup = (
        f"[{color}]{type_}[/{color}]" if color else f"[bold yellow]{type_}[/bold yellow]"
    )
    return (
        f"[dim]{seq:>4}[/dim] [cyan]{ts}[/cyan] "
        f"{type_markup} [dim]{actor}[/dim] {summary}"
    )


def render_log(events: list[dict], *, tail: bool = False) -> list[str]:
    """Return lines for events (newest-last). Tail mode shows last _TAIL_N."""
    shown = events[-_TAIL_N:] if tail else events
    return [render_event(ev) for ev in shown]


def _summarize_inbox_args(args: dict) -> str:
    """A short, single-line preview of an inbox entry's args (T-114)."""
    text = args.get("text") if args else None
    if text:
        return str(text).replace("\n", " ")[:80]
    if args:
        return _json.dumps(args)[:80]
    return ""


def render_inbox(entries: list[dict], cursor: int) -> list[str]:
    """Format every inbox entry (T-114) -- both processed and pending -- as a
    Rich markup line, oldest-first, with the processed/pending split exactly
    at ``cursor`` (index < cursor is processed, same split ``inbox.pending``
    uses)."""
    lines = []
    for i, entry in enumerate(entries):
        processed = i < cursor
        state = "processed" if processed else "pending"
        color = "dim" if processed else "bold yellow"
        ts = (entry.get("ts") or "")[:19]
        command = _esc_log(str(entry.get("command", _EM)))
        summary = _esc_log(_summarize_inbox_args(entry.get("args") or {}))
        lines.append(
            f"[{color}]{state.upper():>9}[/{color}] [cyan]{ts}[/cyan] "
            f"{command}  [dim]{summary}[/dim]"
        )
    return lines


def _esc_log(s: str) -> str:
    """Escape Rich markup chars in user/agent generated content."""
    return s.replace("\\", "\\\\").replace("[", "\\[")


def render_log_line(obj: dict) -> list[str]:
    """Convert one stream-json event object to Rich markup lines for the logs pane."""
    type_ = obj.get("type")
    if type_ == "assistant":
        lines = []
        for block in obj.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text":
                text = block["text"].rstrip()
                if text:
                    lines.append(_esc_log(text))
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input") or {}
                inp_str = _esc_log(_json.dumps(inp)[:100])
                lines.append(f"[dim bold]▶ {name}[/dim bold] [dim]{inp_str}[/dim]")
        return lines
    if type_ == "result":
        classified = classify_result(obj)
        dur = obj.get("duration_ms")
        suffix = f" ({dur}ms)" if dur else ""
        if classified["outcome"] == "success":
            return [f"[green]── {classified['subtype']}{suffix}[/green]"]
        parts = [classified["outcome"]]
        if classified["api_error_status"] is not None:
            parts.append(str(classified["api_error_status"]))
        if classified["message"]:
            parts.append(_esc_log(classified["message"][:200]))
        return [f"[red]── {' '.join(parts)}{suffix}[/red]"]
    if type_ == "rate_limit_event":
        info = obj.get("rate_limit_info") or {}
        kind = info.get("rateLimitType", "")
        status = info.get("status", "")
        resets_at = format_resets_at(info.get("resetsAt"))
        return [f"[yellow]── rate_limit:{kind} status={status} resetsAt={resets_at}[/yellow]"]
    return []


def render_opencode_log_line(obj: dict) -> list[str]:
    """Convert one opencode.jsonl record (OC-5) to Rich markup lines for the logs
    pane -- the tool_use/text vocabulary mirrors ``render_log_line`` above but
    keyed on opencode's own type/part shape (``steplog.oc_part``). Falls back to
    any bare ``text`` field for a record outside the verified vocabulary (RF-3:
    an unrecognized opencode shape must still render SOMETHING, never blank)."""
    type_, part = oc_part(obj)
    if type_ in OC_TEXT_TYPES:
        text = (part.get("text") or "").rstrip()
        return [_esc_log(text)] if text else []
    if type_ in OC_TOOL_USE_TYPES:
        name = part.get("tool", "?")
        summary = _esc_log(oc_summary(name, part))
        return [f"[dim bold]▶ {name}[/dim bold] [dim]{summary}[/dim]"]
    if type_ in OC_STEP_FINISH_TYPES:
        reason = part.get("reason")
        if not reason:
            return []
        color = "green" if reason != "error" else "red"
        return [f"[{color}]── {_esc_log(reason)}[/{color}]"]
    if type_ in OC_STEP_START_TYPES:
        return []
    text = obj.get("text")
    return [_esc_log(text)] if isinstance(text, str) and text else []


def render_pi_log_line(obj: dict) -> list[str]:
    """Convert one pi.jsonl record (T-58) to Rich markup lines for the logs
    pane -- the tool-call/text vocabulary mirrors ``render_log_line`` /
    ``render_opencode_log_line`` above but keyed on pi's own ``AgentEvent``
    shape (``docs/json.md`` in ``@earendil-works/pi-coding-agent``, verified
    against a real captured stream). A tool call renders on
    ``tool_execution_start`` (call intent, matching the other two renderers'
    "render at call time" choice); an assistant turn's text (and, on an
    errored turn, its ``errorMessage``) renders once on that turn's own
    ``message_end`` -- ``message_update`` deltas are deliberately skipped here,
    the same way ``render_log_line`` never renders Claude's own streaming
    deltas, so the pane shows one settled line per turn, not every partial."""
    type_ = obj.get("type")
    if type_ == PI_TOOL_START_TYPE:
        name = obj.get("toolName", "?")
        args = obj.get("args") or {}
        summary = _esc_log(pi_summary(name, args))
        return [f"[dim bold]▶ {name}[/dim bold] [dim]{summary}[/dim]"]
    if type_ == PI_MESSAGE_END_TYPE:
        msg = obj.get("message") or {}
        if msg.get("role") != "assistant":
            return []
        lines = []
        for block in msg.get("content") or []:
            if block.get("type") == "text":
                text = (block.get("text") or "").rstrip()
                if text:
                    lines.append(_esc_log(text))
        if msg.get("stopReason") == "error":
            err = _esc_log((msg.get("errorMessage") or "")[:200])
            lines.append(f"[red]── error {err}[/red]")
        return lines
    if type_ == PI_AGENT_END_TYPE:
        return ["[green]── done[/green]"]
    return []


# --- board-wide activity ticker (T-173) -------------------------------------

CATEGORY_NAMES = {1: "lifecycle", 2: "human", 3: "vcs/ci", 4: "evidence",
                  5: "work", 6: "other"}
_OTHER_CATEGORY = 6

CATEGORY_OF = {
    **dict.fromkeys((ev_types.TICKET_CREATED, ev_types.SPEC_OBSERVED, ev_types.PHASE_CHANGED,
                     ev_types.FINALIZED, ev_types.FAILED, ev_types.STALLED), 1),
    **dict.fromkeys((ev_types.QUESTION_ASKED, ev_types.QUESTION_ANSWERED,
                     ev_types.COMMAND_RECEIVED, ev_types.FAST_PATH_DECIDED,
                     ev_types.APPROVED), 2),
    **dict.fromkeys((ev_types.PR_OPENED, ev_types.PR_UPDATED, ev_types.CI_OBSERVED,
                     ev_types.CI_RERUN_REQUESTED, ev_types.REVIEW_FEEDBACK_RECEIVED,
                     ev_types.REVIEW_REPLY_POSTED, ev_types.RESTACK_QUEUED,
                     ev_types.RESTACK_COMPLETED), 3),
    **dict.fromkeys((ev_types.AC_VERIFIED, ev_types.TEST_RUN_CAPTURED,
                     ev_types.AC_CHECK_CAPTURED, ev_types.AC_QA_VERDICT,
                     ev_types.POST_QA_SKILL_SPAWNED, ev_types.RESEARCH_PROPOSED), 4),
    **dict.fromkeys((ev_types.IMPL_TURN, ev_types.IMPL_STEP, ev_types.NOTE), 5),
    **dict.fromkeys((ev_types.REQUEUE_SCHEDULED, ev_types.CHECKED, ev_types.JIRA_SYNCED,
                     ev_types.LINEAR_SYNCED, ev_types.LINEAR_STATUS_PUSHED), 6),
}


def category_of(type_: str) -> int:
    """Activity group (1-6) of an event type; an unknown type is group 6."""
    return CATEGORY_OF.get(type_, _OTHER_CATEGORY)


def event_summary(ev: dict) -> str:
    """Plain-text payload summary, built the way ``render_event`` builds it."""
    payload = ev.get("payload") or {}
    if ev.get("type") == _IMPL_STEP:
        return f"{payload.get('kind', 'note')} {str(payload.get('summary', ''))[:80]}"
    return ", ".join(f"{k}={v}" for k, v in list(payload.items())[:3])


def _complete_lines(data: bytes) -> tuple[list[dict], int]:
    """Parse the complete (``\\n``-terminated) lines of ``data``; return the
    events and the byte length consumed. Unparseable complete lines are skipped."""
    end = data.rfind(b"\n") + 1
    out: list[dict] = []
    for raw in data[:end].split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = _json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("seq"), int):
            out.append(obj)
    return out, end


class EventTail:
    """Incremental, read-only reader of every ``events/<KEY>.jsonl`` under a home.

    Per key it keeps ``(inode, offset, max_seq)``. ``poll()`` returns events not
    yet returned, each exactly once, surviving ``ops.compact`` (inode change /
    shrink -> re-read archive + new active log, filtered by ``max_seq``) and torn
    last lines (only ``\\n``-terminated lines are consumed)."""

    def __init__(self, home: Path) -> None:
        self._home = Path(home)
        self._state: dict[str, tuple[int | None, int, int]] = {}

    def _keys(self) -> list[str]:
        d = self._home / "events"
        if not d.is_dir():
            return []
        return sorted(f.name[:-len(".jsonl")] for f in d.iterdir()
                      if f.name.endswith(".jsonl") and not f.name.endswith(".archive.jsonl")
                      and not f.name.startswith("."))

    def backfill(self, limit: int = 200) -> list[dict]:
        """The last ``limit`` events board-wide by ``ts`` (oldest first). Seeds each
        key's ``max_seq`` and starts its tail at offset 0 so anything appended after
        this read is still picked up by the first ``poll()``."""
        found: list[dict] = []
        for key in self._keys():
            try:
                inode = store.events_path(self._home, key).stat().st_ino  # before the read
                evs = event_log.read(self._home, key)
            except (OSError, ValueError):
                continue
            self._state[key] = (inode, 0, max((e["seq"] for e in evs), default=0))
            found += [{**e, "key": key} for e in evs]
        found.sort(key=lambda e: (e.get("ts") or "", e["key"], e["seq"]))
        return found[-limit:]

    def poll(self) -> list[dict]:
        out: list[dict] = []
        live = set(self._keys())
        for gone in set(self._state) - live:
            del self._state[gone]
        for key in sorted(live):
            try:
                out += self._poll_key(key)
            except (OSError, ValueError):
                continue  # vanished mid-poll (archived) or invalid key; next tick decides
        out.sort(key=lambda e: (e.get("ts") or "", e["key"], e["seq"]))
        return out

    def _poll_key(self, key: str) -> list[dict]:
        inode, offset, max_seq = self._state.get(key, (None, 0, 0))
        path = store.events_path(self._home, key)
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            rotated = (inode is not None and st.st_ino != inode) or st.st_size < offset
            if rotated:
                data = fh.read()  # active first, archive second: a racing compaction only duplicates
                offset = 0
            else:
                fh.seek(offset)
                data = fh.read()
        evs, consumed = _complete_lines(data)
        if rotated:
            merged = {e["seq"]: e for e in store.read_jsonl(store.events_archive_path(self._home, key))
                      if isinstance(e.get("seq"), int)}
            merged.update((e["seq"], e) for e in evs)
            evs = [merged[s] for s in sorted(merged)]
        fresh = [{**e, "key": key} for e in evs if e["seq"] > max_seq]
        self._state[key] = (st.st_ino, offset + consumed,
                            max([max_seq] + [e["seq"] for e in fresh]))
        return fresh
