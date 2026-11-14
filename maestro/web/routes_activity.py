"""Activity page of `maestro web`: the read-only, filterable, live cross-ticket event feed."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse

from ..view import activity as activity_view
from ..view.events import CATEGORY_NAMES
from .shell import Shell


def _selected(cat: list[int]) -> set[int]:
    """The category numbers asked for; none given (or none valid) means every category."""
    chosen = {c for c in cat if c in CATEGORY_NAMES}
    return chosen or set(CATEGORY_NAMES)


def _qs(cats: set[int], q: str) -> str:
    pairs = [("cat", c) for c in sorted(cats)] if cats != set(CATEGORY_NAMES) else []
    if q:
        pairs.append(("q", q))
    return urlencode(pairs)


def activity_ctx(cat: list[int], q: str) -> dict:
    cats = _selected(cat)
    toggles = []
    for n, name in CATEGORY_NAMES.items():
        # Toggling the last selected category off resets to every category, never an empty feed.
        after = (cats - {n}) if n in cats else (cats | {n})
        toggles.append({"n": n, "name": name, "on": n in cats, "qs": _qs(after or set(CATEGORY_NAMES), q)})
    return {"cats": cats, "q": q, "toggles": toggles, "qs": _qs(cats, q)}


def register(app: FastAPI, shell: Shell) -> None:
    @app.get("/activity", response_class=HTMLResponse)
    def activity(request: Request, cat: list[int] = Query([]), q: str = Query("")):
        ctx = activity_ctx(cat, q)
        rows = activity_view.recent(shell.home, cats=ctx["cats"], text=q)
        return shell.render(request, "activity.html", nav="activity", rows=rows, **ctx)

    @app.get("/activity/rows", response_class=HTMLResponse)
    def activity_rows(request: Request, cat: list[int] = Query([]), q: str = Query("")):
        ctx = activity_ctx(cat, q)
        rows = activity_view.recent(shell.home, cats=ctx["cats"], text=q)
        return shell.render(request, "_activity_rows.html", rows=rows, **ctx)
