"""Event-timeline rendering — no textual dependency, importable in tests."""
from __future__ import annotations

import json as _json

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
        sub = obj.get("subtype", "")
        dur = obj.get("duration_ms")
        suffix = f" ({dur}ms)" if dur else ""
        color = "green" if sub == "success" else "red"
        return [f"[{color}]── {sub}{suffix}[/{color}]"]
    return []
