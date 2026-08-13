"""Event-timeline rendering — no textual dependency, importable in tests."""
from __future__ import annotations

import json as _json

from ..steplog import (OC_STEP_FINISH_TYPES, OC_STEP_START_TYPES,
                        OC_TEXT_TYPES, OC_TOOL_USE_TYPES, classify_result,
                        format_resets_at, oc_part, oc_summary)

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
