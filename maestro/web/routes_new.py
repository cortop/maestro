"""`maestro web` page and guarded POST routes that start work: queue a ticket, review a PR, import from Linear."""
from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from .. import human_actions, store
from .routes_actions import Nudge
from .shell import Shell


def _form(body: bytes) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}


def register(app: FastAPI, shell: Shell, nudge: Nudge | None, vcs=None, tracker=None) -> None:
    @app.get("/new", response_class=HTMLResponse)
    def new_page(request: Request):
        return shell.render(request, "new.html", nav="tickets", result=None)

    def respond(request: Request, kind: str, fn) -> HTMLResponse:
        result: dict = {"kind": kind}
        try:
            result.update(fn())
        except store.MaestroError as e:
            result["error"] = str(e)
        resp = shell.render(request, "_new_result.html", result=result)
        if "error" in result:
            resp.status_code = 409
        return resp

    def create(request: Request, f: dict) -> HTMLResponse:
        def go() -> dict:
            try:
                priority = int(f.get("priority", "3") or "3")
            except ValueError:
                raise store.MaestroError("priority must be a number") from None
            entry = human_actions.queue_ticket(shell.cfg(), f.get("title", ""), priority=priority,
                                               nudge=nudge)
            return {"queued": True, "title": entry["title"], "priority": priority}
        return respond(request, "create", go)

    def review(request: Request, f: dict) -> HTMLResponse:
        def go() -> dict:
            res = human_actions.start_review(shell.cfg(), f.get("url", ""), vcs=vcs)
            if res.get("started"):
                _nudge(res["key"])
            return {"key": res["key"], "started": bool(res.get("started")),
                    "reason": res.get("reason")}
        return respond(request, "review", go)

    def _nudge(key: str) -> None:
        if nudge is not None:
            nudge(key)

    def import_(request: Request, f: dict) -> HTMLResponse:
        def go() -> dict:
            res = human_actions.import_linear(shell.cfg(), f.get("ref", ""), tracker=tracker)
            if res.get("minted"):
                _nudge(res["key"])
            return {"key": res["key"], "minted": bool(res.get("minted"))}
        return respond(request, "import", go)

    def make(fn):
        async def handler(request: Request) -> HTMLResponse:
            return await run_in_threadpool(fn, request, _form(await request.body()))
        handler.__name__ = f"new_{fn.__name__}"
        return handler

    shell.guard.post(app, "/new/create", make(create), response_class=HTMLResponse)
    shell.guard.post(app, "/new/review", make(review), response_class=HTMLResponse)
    shell.guard.post(app, "/new/import", make(import_), response_class=HTMLResponse)
