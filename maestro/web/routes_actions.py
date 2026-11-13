"""Guarded POST routes of `maestro web` for per-ticket human actions; each returns the refreshed fragment."""
from __future__ import annotations

import threading
from typing import Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from .. import human_actions, ops, store
from .. import snapshot as snap_mod
from ..view import ticket as ticket_view
from . import routes_now, routes_ticket
from .shell import Shell

Nudge = Callable[[str | None], object]


def default_nudge(shell: Shell) -> Nudge:
    """A nudge that runs one dispatch sweep on a daemon thread, honouring `nudge_on_human_input`."""
    def _nudge(_key: str | None) -> None:
        def _run() -> None:
            try:
                from .. import cli
                cfg = shell.cfg()
                if cfg.nudge_on_human_input:
                    cli._nudge(cfg)
            except Exception:  # a failed nudge never fails the action; the next sweep picks it up
                pass
        threading.Thread(target=_run, daemon=True).start()
    return _nudge


class Refused(store.MaestroError):
    """An action the current state or input does not allow (answered 409)."""


def _form(body: bytes) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}


def register(app: FastAPI, shell: Shell, nudge: Nudge | None, vcs=None) -> None:
    home = shell.home

    def need_text(f: dict, name: str = "text") -> str:
        text = f.get(name, "").strip()
        if not text:
            raise Refused(f"{name} must not be empty")
        return text

    def need_confirm(f: dict, what: str) -> None:
        if f.get("confirm") != "1":
            raise Refused(f"{what} needs confirmation")

    def do_answer(key: str, f: dict) -> None:
        qid = f.get("qid") or None
        if f.get("accept") == "1":
            snap = snap_mod.load(home, key)
            text = snap.open_questions.get(qid or "")
            rec = ops.parse_round_question(text)[3] if text else None
            if not rec:
                raise Refused(f"{key}: this question carries no recommendation to accept")
            human_actions.answer(home, key, rec, qid=qid, accepted_recommendation=True, nudge=nudge)
        else:
            human_actions.answer(home, key, need_text(f), qid=qid, nudge=nudge)

    def do_message(key: str, f: dict) -> None:
        human_actions.message(home, key, need_text(f), nudge=nudge)

    def do_retry(key: str, f: dict) -> None:
        note = f.get("text", "").strip()
        human_actions.retry(home, key, nudge=None if note else nudge)  # refuses non-degraded first
        if note:
            human_actions.message(home, key, note, nudge=nudge)

    def do_discard(key: str, f: dict) -> None:
        need_confirm(f, "discard")
        human_actions.discard(home, key, nudge=nudge)

    def do_hold(key: str, f: dict) -> None:
        human_actions.hold(home, key, reason=f.get("text", "").strip() or None)

    def do_release(key: str, f: dict) -> None:
        human_actions.release(home, key)

    def do_stop(key: str, f: dict) -> None:
        need_confirm(f, "stop")
        human_actions.stop(shell.cfg(), key)

    actions = {"answer": do_answer, "message": do_message, "retry": do_retry,
               "discard": do_discard, "hold": do_hold, "release": do_release, "stop": do_stop}

    def ticket_exists(key: str) -> bool:
        try:
            return ticket_view.exists(home, key)
        except store.MaestroError:
            return False

    def run(request: Request, key: str, name: str, f: dict) -> HTMLResponse:
        error = None
        try:
            if not ticket_exists(key):
                raise Refused(f"no such ticket: {key}")
            actions[name](key, f)
        except store.MaestroError as e:
            error = {"key": key, "msg": str(e)}
        extra = {"action_error": error}
        if f.get("ctx") == "now":
            resp = shell.render(request, "_now_decide.html", **routes_now.now_ctx(shell, **extra))
        else:
            resp = shell.render(request, "_ticket_head.html",
                                **routes_ticket.head_ctx(shell, key, **extra))
        if error:
            resp.status_code = 409
        return resp

    def make(name: str):
        async def handler(request: Request, key: str) -> HTMLResponse:
            f = _form(await request.body())
            return await run_in_threadpool(run, request, key, name, f)
        handler.__name__ = f"act_{name}"
        return handler

    for name in actions:
        shell.guard.post(app, f"/t/{{key}}/{name}", make(name), response_class=HTMLResponse)

    def create_pending(request: Request, key: str, body: bytes) -> HTMLResponse:
        """The one path from a review ticket to GitHub: a pending review of the kept findings."""
        f = _form(body)
        error = None
        try:
            if not ticket_exists(key):
                raise Refused(f"no such ticket: {key}")
            snap = snap_mod.load(home, key)
            if f.get("head") != (snap.review_head_sha or ""):
                raise Refused("the review changed since this page loaded; reload it first")
            kept = [{"index": i, "comment": f.get(f"comment-{i}", "")}
                    for i in range(len(snap.review_comments)) if f.get(f"include-{i}") == "1"]
            human_actions.create_pending_review(shell.cfg(), key, kept, vcs=vcs)
        except store.MaestroError as e:
            error = {"key": key, "msg": str(e)}
        resp = shell.render(request, "_ticket_review.html",
                            **routes_ticket.review_ctx(shell, key, action_error=error))
        if error:
            resp.status_code = 409
        return resp

    async def create_pending_handler(request: Request, key: str) -> HTMLResponse:
        return await run_in_threadpool(create_pending, request, key, await request.body())

    shell.guard.post(app, "/t/{key}/review/pending", create_pending_handler, response_class=HTMLResponse)
