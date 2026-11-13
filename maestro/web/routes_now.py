"""Now page routes (`/`, `/now/*` fragments) of `maestro web`."""
from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .. import projection, store
from ..statemachine import Phase
from ..view import now as now_view
from .shell import Shell


def _landed_today(shell: Shell, now: float) -> int:
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return sum(1 for s in projection.load_snapshots(shell.home)
               if s.phase == Phase.DONE.value and (store.iso_to_epoch(s.updated_ts) or 0) >= midnight)


def now_ctx(shell: Shell, **extra) -> dict:
    now = store.now_epoch()
    view = now_view.build(shell.home, shell.cfg(), now)
    agents = [r for r in view.in_flight if r.owner == "agent"]
    dispatcher = [r for r in view.in_flight if r.owner == "dispatcher"]
    counters = (("agents", len(agents)), ("CI / review", len(dispatcher)),
                ("queued", len(view.queued)), ("landed today", _landed_today(shell, now)))
    return {"view": view, "counters": counters, "needs_count": len({c.key for c in view.needs_you}),
            "agents": agents, "dispatcher": dispatcher, **extra}


def register(app: FastAPI, shell: Shell) -> None:
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return shell.render(request, "now.html", nav="now", **now_ctx(shell))

    @app.get("/now/headline", response_class=HTMLResponse)
    def headline(request: Request):
        return shell.render(request, "_now_headline.html", **now_ctx(shell))

    @app.get("/now/decide", response_class=HTMLResponse)
    def decide(request: Request):
        return shell.render(request, "_now_decide.html", **now_ctx(shell))

    @app.get("/now/ledger", response_class=HTMLResponse)
    def ledger(request: Request):
        return shell.render(request, "_now_ledger.html", **now_ctx(shell))
