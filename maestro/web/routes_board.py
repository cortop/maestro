"""Board page routes (`/tickets`, `/board/rows`) of `maestro web`."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..view import board as board_view
from .shell import Shell


def board_query(filter: str, q: str = "", sort: str = "", desc: bool = False, **extra) -> str:
    """The URL query of a board view; blank search/sort are omitted so default URLs stay short."""
    params = {"filter": filter, **extra}
    if q.strip():
        params["q"] = q.strip()
    if sort:
        params["sort"] = sort
        params["dir"] = "desc" if desc else "asc"
    return urlencode(params)


def sort_query(view: board_view.BoardView, column: str) -> str:
    """Query of the header link for *column*: a second click on the active column reverses it."""
    if view.sort == column:
        desc = not view.desc
    else:
        desc = column in board_view.NUMERIC_SORT
    return board_query(view.filter, view.search, column, desc)


def register(app: FastAPI, shell: Shell) -> None:
    def board_ctx(filter: str, q: str, sort: str, dir: str) -> dict:
        desc = dir == "desc"
        try:
            view = board_view.board(shell.home, shell.cfg(), filter, q, sort, desc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"view": view, "filters": board_view.FILTER_NAMES,
                "columns": [("key", "Key"), ("phase", "Phase"), ("title", "Title"), ("pr", "PR"),
                            ("ci", "CI"), ("fails", "Fails")],
                "sort_query": lambda col: sort_query(view, col),
                "query": board_query(filter, q, sort, desc),
                "filter_query": lambda f: board_query(f, q, sort, desc)}

    @app.get("/tickets", response_class=HTMLResponse)
    def tickets(request: Request, filter: str = "needs-you", q: str = "", sort: str = "",
                dir: str = "asc"):
        return shell.render(request, "board.html", nav="tickets", **board_ctx(filter, q, sort, dir))

    @app.get("/board/rows", response_class=HTMLResponse)
    def board_rows(request: Request, filter: str = "needs-you", q: str = "", sort: str = "",
                   dir: str = "asc"):
        return shell.render(request, "_rows.html", **board_ctx(filter, q, sort, dir))
