"""FastAPI app for `maestro web`: server-rendered pages and htmx fragments over `maestro.view`."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterable

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config as config_mod
from .. import store
from ..config import Config
from ..view import board as board_view
from ..view import events as ev_view
from ..view import ticket as ticket_view
from .stream import Hub

_HERE = Path(__file__).parent


def _cfg(home: Path) -> Config:
    try:
        return config_mod.load(str(home))
    except store.MaestroError:
        return Config(home=home)


def create_app(home: Path | str, *, poll_interval: float = 1.0,
               stream_limit: int | None = None) -> FastAPI:
    """Build the app over *home*. *stream_limit* ends each `/stream` after that many events
    (tests; a browser stream is unbounded)."""
    home = Path(home)
    hub = Hub(home, poll_interval)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(title="maestro", lifespan=lifespan)
    app.state.home = home
    app.state.hub = hub
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    templates.env.filters["dur"] = ev_view.fmt_duration

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"home": str(home.resolve()), **ctx})

    def board_ctx(filter: str) -> dict:
        try:
            view = board_view.board(home, _cfg(home), filter)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"view": view, "filters": board_view.FILTER_NAMES}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, filter: str = "needs-you"):
        return render(request, "board.html", **board_ctx(filter))

    @app.get("/board/rows", response_class=HTMLResponse)
    def board_rows(request: Request, filter: str = "needs-you"):
        return render(request, "_rows.html", **board_ctx(filter))

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
        return render(request, "ticket.html", t=tv, page=page, key=key)

    @app.get("/t/{key}/events", response_class=HTMLResponse)
    def ticket_events(request: Request, key: str, before: int | None = Query(None)):
        require(key)
        page = ticket_view.event_page(home, key, before)
        return render(request, "_events.html", page=page, key=key, summary=ev_view.event_summary)

    @app.get("/stream", response_class=EventSourceResponse)
    async def stream(last_event_id: str | None = Header(None)) -> AsyncIterable[ServerSentEvent]:
        q = hub.subscribe()
        sent = 0
        try:
            if last_event_id is not None:
                for ev in hub.full_refresh():
                    yield _sse(ev)
                    sent += 1
                    if stream_limit and sent >= stream_limit:
                        return
            while True:
                ev = await q.get()
                yield _sse(ev)
                sent += 1
                if stream_limit and sent >= stream_limit:
                    return
        except asyncio.CancelledError:
            raise
        finally:
            hub.unsubscribe(q)

    return app


def _sse(ev: tuple[int, str]) -> ServerSentEvent:
    return ServerSentEvent(raw_data=json.dumps({"event": ev[1]}), event=ev[1], id=str(ev[0]))


def main(args) -> int:
    import uvicorn

    cfg = config_mod.load(getattr(args, "home", None))
    uvicorn.run(create_app(cfg.home), host=args.host, port=args.port, log_level="warning")
    return 0
