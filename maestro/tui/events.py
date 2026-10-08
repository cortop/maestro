"""Event-timeline rendering — no textual dependency, importable in tests."""
from __future__ import annotations

import json as _json
from dataclasses import dataclass

from rich.text import Text

from .. import store
from ..statemachine import Phase
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


def event_summary(ev: dict) -> str:
    """Plain-text one-line summary of an event's payload (no markup, for table cells)."""
    payload = ev.get("payload") or {}
    if ev.get("type") == _IMPL_STEP:
        return str(payload.get("summary", ""))[:80]
    return ", ".join(f"{k}={v}" for k, v in list(payload.items())[:3])


def event_row(ev: dict) -> tuple[Text, Text, Text, Text, Text]:
    """One event as ``(seq, ts, type, actor, summary)`` Text cells -- built from plain
    text so a ``[`` in a payload is never parsed as markup by the DataTable."""
    type_ = ev.get("type", _EM)
    color = _MILESTONE_COLOR.get(type_, "bold yellow")
    return (
        Text(str(ev.get("seq", "?")), style="dim", justify="right"),
        Text((ev.get("ts") or "")[:19], style="cyan"),
        Text(str(type_), style=color),
        Text(str(ev.get("actor", _EM)), style="dim"),
        Text(event_summary(ev).replace("\n", " ")),
    )


@dataclass(frozen=True)
class DwellSegment:
    phase: str
    seconds: float
    forced: bool = False
    current: bool = False


def phase_dwell(events: list[dict], now: float) -> list[DwellSegment]:
    """Fold the phase-moving events into per-phase dwell segments (oldest first).

    Mirrors the fold arms in ``snapshot``: TicketCreated -> triaging, PhaseChanged
    (valid ``phase``) -> that phase, Stalled -> degraded, Finalized -> done. An unknown
    phase and a move to the phase already current are no-ops, and ``done`` absorbs.
    The last segment is ``current`` and runs up to *now*.
    """
    moves: list[tuple[str, float, bool]] = []
    for ev in events:
        t = ev.get("type")
        payload = ev.get("payload") or {}
        forced = False
        if t == "TicketCreated":
            phase = Phase.TRIAGING.value
        elif t == "PhaseChanged":
            try:
                phase = Phase(payload.get("phase")).value
            except ValueError:
                continue
            forced = bool(payload.get("forced_by"))
        elif t == "Stalled":
            phase = Phase.DEGRADED.value
        elif t == "Finalized":
            phase = Phase.DONE.value
        else:
            continue
        ts = store.iso_to_epoch(ev.get("ts"))
        if ts is None:
            continue
        if moves:
            if moves[-1][0] == Phase.DONE.value or moves[-1][0] == phase:
                continue
        moves.append((phase, ts, forced))
    segs: list[DwellSegment] = []
    for i, (phase, ts, forced) in enumerate(moves):
        last = i == len(moves) - 1
        end = now if last else moves[i + 1][1]
        segs.append(DwellSegment(phase, max(0.0, end - ts), forced, last))
    return segs


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def render_dwell(segs: list[DwellSegment], width: int = 100) -> Text:
    """One-line ``implementing 38m ▸ qa 12m ▸ in-review 2h14m (now)`` strip; forced hops
    red. Middle segments are elided (``…``) until the line fits *width*."""
    def seg_text(seg: DwellSegment) -> Text:
        label = f"{seg.phase} {fmt_duration(seg.seconds)}" + (" (now)" if seg.current else "")
        return Text(label, style="red" if seg.forced else "")

    sep = " ▸ "
    parts = [seg_text(s) for s in segs]
    n = len(parts)

    def build(head: int, tail: int) -> Text:
        items: list[Text | None] = [*parts[:head]]
        if head + tail < n:
            items.append(None)
        items += parts[n - tail:] if tail else []
        out = Text()
        for i, item in enumerate(items):
            if i:
                out.append(sep)
            if item is None:
                out.append("…", style="dim")
            else:
                out.append_text(item)
        return out

    line = build(n, 0)
    if n <= 2 or line.cell_len <= width:
        return line
    # Elide the middle, keeping the oldest and newest, as little as fits.
    for kept in range(n - 1, 1, -1):
        for tail in range(kept - 1, 0, -1):
            line = build(kept - tail, tail)
            if line.cell_len <= width:
                return line
    return build(1, 1)


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
