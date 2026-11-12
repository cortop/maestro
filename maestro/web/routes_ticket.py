"""Ticket detail routes (`/t/{key}` and its fragments) of `maestro web`."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .. import store
from ..view import events as ev_view
from ..view import logs as logs_view
from ..view import rounds as rounds_view
from ..view import ticket as ticket_view
from .shell import Shell


def register(app: FastAPI, shell: Shell) -> None:
    home = shell.home

    def require(key: str) -> None:
        try:
            ok = ticket_view.exists(home, key)
        except store.MaestroError:
            ok = False
        if not ok:
            raise HTTPException(status_code=404, detail=f"no such ticket: {key}")

    @app.get("/t/{key}", response_class=HTMLResponse)
    def ticket_page(request: Request, key: str):
        require(key)
        tv = ticket_view.ticket(home, key, store.now_epoch())
        page = ticket_view.event_page(home, key)
        tr = rounds_view.build(home, key)
        return shell.render(request, "ticket.html", nav="tickets", t=tv, page=page, key=key, tr=tr)

    @app.get("/t/{key}/events", response_class=HTMLResponse)
    def ticket_events(request: Request, key: str, before: int | None = Query(None)):
        require(key)
        page = ticket_view.event_page(home, key, before)
        return shell.render(request, "_events.html", page=page, key=key,
                            summary=ev_view.event_summary)

    @app.get("/t/{key}/rounds", response_class=HTMLResponse)
    def ticket_rounds(request: Request, key: str):
        require(key)
        return shell.render(request, "_rounds.html", tr=rounds_view.build(home, key))

    @app.get("/t/{key}/logs", response_class=HTMLResponse)
    def ticket_logs(request: Request, key: str):
        require(key)
        return shell.render(request, "_logs.html", lg=logs_view.build(home, key),
                            detail=lambda p: logs_view.detail_lines(home, key, p))
