"""Board page routes (`/tickets`, `/board/rows`) of `maestro web`."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..view import board as board_view
from .shell import Shell


def register(app: FastAPI, shell: Shell) -> None:
    def board_ctx(filter: str) -> dict:
        try:
            view = board_view.board(shell.home, shell.cfg(), filter)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"view": view, "filters": board_view.FILTER_NAMES}

    @app.get("/tickets", response_class=HTMLResponse)
    def tickets(request: Request, filter: str = "needs-you"):
        return shell.render(request, "board.html", nav="tickets", **board_ctx(filter))

    @app.get("/board/rows", response_class=HTMLResponse)
    def board_rows(request: Request, filter: str = "needs-you"):
        return shell.render(request, "_rows.html", **board_ctx(filter))
