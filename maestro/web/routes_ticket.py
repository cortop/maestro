"""Ticket detail routes (`/t/{key}` and its fragments) of `maestro web`."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .. import claims, fleet, gates, store
from ..statemachine import TERMINAL_PHASES
from ..view import events as ev_view
from ..view import logs as logs_view
from ..view import rounds as rounds_view
from ..view import ticket as ticket_view
from .shell import Shell


def head_ctx(shell: Shell, key: str, **extra) -> dict:
    """Context of the ticket head: the view plus which actions are valid for its state."""
    home = shell.home
    now = store.now_epoch()
    cfg = shell.cfg()
    tv = ticket_view.ticket(home, key, now)
    runner = gates.spec_runner(home, key)[0] or cfg.runner
    live = tv.snap.phase not in {p.value for p in TERMINAL_PHASES}
    claim = claims.read_claim(home, key)
    can_stop = bool(claim) and claim.get("kind") not in ("testrun", "restack")
    return {"t": tv, "key": key, "priority": gates.spec_priority(home, key),
            "repo": tv.snap.repo or "default", "runner": runner,
            "card": ticket_view.dispatch_card(home, key, tv.snap, cfg, now),
            "can_message": live, "can_hold": live and fleet.hold_state(home, key, now) is None,
            "can_release": fleet.hold_state(home, key, now) is not None,
            "can_stop": can_stop, **extra}


def register(app: FastAPI, shell: Shell) -> None:
    home = shell.home

    def require(key: str) -> None:
        try:
            ok = ticket_view.exists(home, key)
        except store.MaestroError:
            ok = False
        if not ok:
            raise HTTPException(status_code=404, detail=f"no such ticket: {key}")

    def live_steps(key: str) -> list[str]:
        """Step digest lines of the session in progress (memoized by log size and mtime)."""
        lg = logs_view.build(home, key)
        if not lg.live:
            return []
        return rounds_view.session_lines(home, key, lg.live)[1:]

    @app.get("/t/{key}", response_class=HTMLResponse)
    def ticket_page(request: Request, key: str):
        require(key)
        return shell.render(request, "ticket.html", nav="tickets", **head_ctx(shell, key))

    @app.get("/t/{key}/head", response_class=HTMLResponse)
    def ticket_head(request: Request, key: str):
        require(key)
        return shell.render(request, "_ticket_head.html", **head_ctx(shell, key))

    @app.get("/t/{key}/side", response_class=HTMLResponse)
    def ticket_side(request: Request, key: str):
        require(key)
        return shell.render(request, "_ticket_side.html", **head_ctx(shell, key))

    @app.get("/t/{key}/spec", response_class=HTMLResponse)
    def ticket_spec(request: Request, key: str):
        require(key)
        return shell.render(request, "_ticket_text.html", id="spec",
                            text=ticket_view.read_text(store.spec_path(home, key)))

    @app.get("/t/{key}/proposal", response_class=HTMLResponse)
    def ticket_proposal(request: Request, key: str):
        require(key)
        return shell.render(request, "_ticket_text.html", id="proposal",
                            text=ticket_view.read_text(store.ticket_dir(home, key) / "proposal.md"))

    @app.get("/t/{key}/events", response_class=HTMLResponse)
    def ticket_events(request: Request, key: str, before: int | None = Query(None)):
        require(key)
        page = ticket_view.event_page(home, key, before)
        return shell.render(request, "_events.html", page=page, key=key, wrap=before is None,
                            summary=ev_view.event_summary)

    @app.get("/t/{key}/rounds", response_class=HTMLResponse)
    def ticket_rounds(request: Request, key: str):
        require(key)
        return shell.render(request, "_rounds.html", tr=rounds_view.build(home, key),
                            live=live_steps(key))

    @app.get("/t/{key}/logs", response_class=HTMLResponse)
    def ticket_logs(request: Request, key: str):
        require(key)
        return shell.render(request, "_logs.html", lg=logs_view.build(home, key),
                            detail=lambda p: logs_view.detail_lines(home, key, p))
