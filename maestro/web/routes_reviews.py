"""Reviews page of `maestro web`: the live list of inbound PR reviews, with the start, dismiss and retry actions."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from .. import human_actions, ops, store
from ..view import reviews as reviews_view
from .routes_actions import Nudge
from .routes_new import _form
from .shell import Shell

FILTERS = ("open", "needs", "progress", "github", "history")
_DAY = 86400


def when(iso: str | None, now: float) -> str:
    """A timestamp as an age while recent ("12 min ago"), else a short weekday-and-time."""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    age = max(0.0, now - dt.timestamp())
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{int(age // 60)} min ago"
    if age < _DAY:
        return f"{int(age // 3600)} h ago"
    return dt.strftime("%a %H:%M" if age < 7 * _DAY else "%Y-%m-%d %H:%M")


def ask_to_brief(row) -> str:
    """Time from the request to the close, or n/a without a recorded request."""
    try:
        start = datetime.fromisoformat(row.requested_at)
        end = datetime.fromisoformat(row.closed_at)
    except (TypeError, ValueError):
        return "n/a"
    if row.requested_at_inferred:
        return "n/a"
    s = max(0, int(((end if end.tzinfo else end.replace(tzinfo=timezone.utc))
                    - (start if start.tzinfo else start.replace(tzinfo=timezone.utc))).total_seconds()))
    return f"{s // 60} min" if s >= 60 else f"{s}s"


def reviews_ctx(shell: Shell, filter_: str, **extra) -> dict:
    now = store.now_epoch()
    filter_ = filter_ if filter_ in FILTERS else "open"
    view = reviews_view.build(shell.home, shell.cfg(), now)
    c = view.counts
    counts = {"open": c["needs_you"] + c["in_progress"] + c["on_github"], "needs": c["needs_you"],
              "progress": c["in_progress"], "github": c["on_github"], "history": c["history"]}
    return {"view": view, "filter": filter_, "counts": counts, "now": now, "when": when,
            "ask_to_brief": ask_to_brief,
            "show_needs": filter_ in ("open", "needs"),
            "show_table": filter_ in ("open", "progress", "github"),
            "table_rows": (view.in_progress if filter_ in ("open", "progress") else []) +
                          (view.on_github if filter_ in ("open", "github") else []),
            "show_history": filter_ == "history", "msg": None, "error": None, **extra}


def register(app: FastAPI, shell: Shell, nudge: Nudge | None, vcs=None) -> None:
    @app.get("/reviews", response_class=HTMLResponse)
    def reviews(request: Request, filter: str = Query("open")):
        return shell.render(request, "reviews.html", nav="reviews", **reviews_ctx(shell, filter))

    @app.get("/reviews/rows", response_class=HTMLResponse)
    def reviews_rows(request: Request, filter: str = Query("open")):
        return shell.render(request, "_reviews_rows.html", **reviews_ctx(shell, filter))

    def respond(request: Request, filter_: str, fn) -> HTMLResponse:
        msg = error = None
        try:
            msg = fn()
        except store.MaestroError as e:
            error = str(e)
        resp = shell.render(request, "_reviews_rows.html", **reviews_ctx(shell, filter_, msg=msg, error=error))
        if error:
            resp.status_code = 409
        return resp

    def make(fn):
        async def handler(request: Request, key: str | None = None) -> HTMLResponse:
            f = _form(await request.body())
            return await run_in_threadpool(respond, request, f.get("filter", "open"), lambda: fn(f, key))
        handler.__name__ = f"reviews_{fn.__name__}"
        return handler

    def start(f: dict, _key) -> str:
        res = human_actions.start_review(shell.cfg(), f.get("url", ""), vcs=vcs, via="web")
        if res.get("started") and nudge is not None:
            nudge(res["key"])
        return f"Review of {res['key']} started." if res.get("started") else (
            res.get("reason") or f"{res['key']} is already being reviewed.")

    def dismiss(f: dict, key) -> str:
        human_actions.mark_reviewed(shell.cfg(), key)
        return f"{key} closed."

    def retry(f: dict, key) -> str:
        ops.retry_review(shell.cfg(), key, vcs=vcs)
        if nudge is not None:
            nudge(key)
        return f"{key} retried."

    shell.guard.post(app, "/reviews/start", make(start), response_class=HTMLResponse)
    shell.guard.post(app, "/reviews/{key}/dismiss", make(dismiss), response_class=HTMLResponse)
    shell.guard.post(app, "/reviews/{key}/retry", make(retry), response_class=HTMLResponse)
