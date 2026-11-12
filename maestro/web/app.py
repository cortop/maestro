"""FastAPI app for `maestro web`: server-rendered pages and htmx fragments over `maestro.view`."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterable

from fastapi import FastAPI, Header
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config as config_mod
from .. import store
from ..config import Config
from ..view import events as ev_view
from . import routes_board, routes_fleet, routes_ticket
from .shell import Shell
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
    shell = Shell(home, _cfg, Jinja2Templates(directory=str(_HERE / "templates")))
    shell.templates.env.filters["dur"] = ev_view.fmt_duration
    routes_board.register(app, shell)
    routes_ticket.register(app, shell)
    routes_fleet.register(app, shell)

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
