"""Event-timeline rendering — no textual dependency, importable in tests."""
from __future__ import annotations

_EM = "—"
_TAIL_N = 20


def render_event(ev: dict) -> str:
    """Format one event as a Rich markup line: seq ts type actor payload-summary."""
    ts = (ev.get("ts") or "")[:19]
    seq = ev.get("seq", "?")
    type_ = ev.get("type", _EM)
    actor = ev.get("actor", _EM)
    payload = ev.get("payload") or {}
    if payload:
        summary = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:3])
    else:
        summary = ""
    return (
        f"[dim]{seq:>4}[/dim] [cyan]{ts}[/cyan] "
        f"[bold yellow]{type_}[/bold yellow] [dim]{actor}[/dim] {summary}"
    )


def render_log(events: list[dict], *, tail: bool = False) -> list[str]:
    """Return lines for events (newest-last). Tail mode shows last _TAIL_N."""
    shown = events[-_TAIL_N:] if tail else events
    return [render_event(ev) for ev in shown]
