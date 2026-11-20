"""Reviews page of `maestro web`: the live list of inbound PR reviews, with the start, dismiss and retry actions."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from .. import human_actions, ops, review as review_pkg, store
from ..review.model import _where_label
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


def clock(iso: str | None) -> str:
    """A timestamp as HH:MM (UTC), or empty when unparseable."""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return (dt.astimezone(timezone.utc) if dt.tzinfo else dt).strftime("%H:%M")


def _span(a: str | None, b: str | None) -> str:
    """Whole minutes (or seconds) from *a* to *b*; empty when either is missing or b is not later."""
    try:
        s = int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
    except (TypeError, ValueError):
        return ""
    return "" if s <= 0 else (f"{s // 60}m" if s >= 60 else f"{s}s")


# Per rail step: its label, then the timeline labels whose first hit starts it.
_RAIL = (("asked", "asked", "filed"), ("tests", "round started"), ("reviewing", "tests done"),
         ("brief ready", "brief ready"), ("on github", "pending created"), ("closed", "closed"))
_RAIL_AT = {"queued": 0, "testing": 1, "reviewing": 2, "brief-ready": 3, "blocked": 3, "stranded": 3,
            "failed": 3, "other": 3, "pending-on-github": 4, "closed": 5}


def rail(row, timeline: list[dict]) -> list[dict]:
    """The six review steps with start time, duration and visited/current flags."""
    last: dict[str, str | None] = {}
    for e in timeline:
        last.setdefault(e["label"], e["ts"])
    cur = _RAIL_AT.get(row.status, 3)
    starts = [next((last[n] for n in names[1:] if n in last), None) for names in _RAIL]
    out = []
    for i, names in enumerate(_RAIL):
        nxt = next((t for t in starts[i + 1:] if t), None)
        out.append({"label": names[0], "time": clock(starts[i]),
                    "span": _span(starts[i], nxt) if i < cur else "",
                    "visited": i <= cur, "current": i == cur, "dispatcher": names[0] == "tests"})
    return out


_TIMELINE_TEXT = {"asked": "Asked", "filed": "Noticed by maestro", "round started": "Review round started",
                  "tests done": "Tests finished", "brief ready": "Brief ready",
                  "pending created": "Pending review created on GitHub", "closed": "Closed"}


def timeline_rows(timeline: list[dict]) -> list[dict]:
    out, prev = [], None
    for e in timeline:
        out.append({"text": _TIMELINE_TEXT.get(e["label"], e["label"]), "time": clock(e["ts"]),
                    "after": _span(prev, e["ts"]) if prev else ""})
        prev = e["ts"]
    return out


def request_rows(requests: list[dict]) -> list[dict]:
    out = []
    for q in requests:
        who = q.get("requester") if isinstance(q.get("requester"), dict) else {}
        where = q.get("where") if isinstance(q.get("where"), dict) else {}
        out.append({"by": who.get("name"), "at": q.get("requested_ts"),
                    "label": _where_label(q.get("via"), where) or q.get("via"),
                    "url": where.get("permalink")})
    return out


def detail_ctx(shell: Shell, key: str, **extra) -> dict:
    now = store.now_epoch()
    d = reviews_view.detail(shell.home, shell.cfg(), key, now)
    if d is None:
        raise HTTPException(status_code=404, detail=f"no such review: {key}")
    r = review_pkg.load(shell.home, key)
    return {"d": d, "row": d.row, "r": r, "key": key, "now": now, "when": when, "clock": clock,
            "rail": rail(d.row, d.timeline), "timeline": timeline_rows(d.timeline),
            "requests": request_rows(d.requests),
            "can_rereview": r.stale and d.row.status == "brief-ready",
            "can_retry": d.row.status == "blocked", "msg": None, "error": None, **extra}


def register(app: FastAPI, shell: Shell, nudge: Nudge | None, vcs=None) -> None:
    @app.get("/reviews", response_class=HTMLResponse)
    def reviews(request: Request, filter: str = Query("open")):
        return shell.render(request, "reviews.html", nav="reviews", **reviews_ctx(shell, filter))

    @app.get("/reviews/rows", response_class=HTMLResponse)
    def reviews_rows(request: Request, filter: str = Query("open")):
        return shell.render(request, "_reviews_rows.html", **reviews_ctx(shell, filter))

    @app.get("/reviews/{key}", response_class=HTMLResponse)
    def review_page(request: Request, key: str):
        return shell.render(request, "review.html", nav="reviews", **detail_ctx(shell, key))

    @app.get("/reviews/{key}/head", response_class=HTMLResponse)
    def review_head(request: Request, key: str):
        return shell.render(request, "_review_head.html", **detail_ctx(shell, key))

    def respond(request: Request, filter_: str, fn, key: str | None = None, page: str = "") -> HTMLResponse:
        msg = error = None
        try:
            msg = fn()
        except store.MaestroError as e:
            error = str(e)
        if page == "detail":
            resp = shell.render(request, "_review_head.html", **detail_ctx(shell, key, msg=msg, error=error))
            if error:
                resp.status_code = 409
            return resp
        resp = shell.render(request, "_reviews_rows.html", **reviews_ctx(shell, filter_, msg=msg, error=error))
        if error:
            resp.status_code = 409
        return resp

    def make(fn):
        async def handler(request: Request, key: str | None = None) -> HTMLResponse:
            f = _form(await request.body())
            return await run_in_threadpool(respond, request, f.get("filter", "open"), lambda: fn(f, key), key, f.get("page", ""))
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
    def rereview(f: dict, key) -> str:
        ops.rereview(shell.cfg(), key, vcs=vcs)
        if nudge is not None:
            nudge(key)
        return f"{key} re-reviewing."

    shell.guard.post(app, "/reviews/{key}/rereview", make(rereview), response_class=HTMLResponse)
    shell.guard.post(app, "/reviews/{key}/retry", make(retry), response_class=HTMLResponse)
