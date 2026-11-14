"""Spec-tab routes of `maestro web`: the guarded POSTs that edit a ticket's spec and fire its post-QA skill."""
from __future__ import annotations

from typing import Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from .. import dispatcher, gates, ops, sessions, store
from .. import snapshot as snap_mod
from ..view import ticket as ticket_view
from .shell import Shell

_CLOSED_PR_STATES = ("merged", "closed")


class Refused(store.MaestroError):
    """An edit the current state or input does not allow (answered 409)."""


def _form(body: bytes) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}


def spec_ctx(shell: Shell, key: str, **extra) -> dict:
    """Context of the Spec tab: the file, its hash, the front-matter values and which controls apply."""
    home = shell.home
    text = ticket_view.read_text(store.spec_path(home, key))
    snap = snap_mod.load(home, key)
    runner, runner_model = gates.spec_runner(home, key)
    open_pr = bool(snap.pr_number) and (snap.pr_state or "open").lower() not in _CLOSED_PR_STATES
    return {"key": key, "id": "spec", "text": text, "spec_hash": dispatcher.spec_hash_on_disk(home, key) or "",
            "editor_text": text, "priority": gates.spec_priority(home, key),
            "depends_on": ", ".join(dispatcher.parse_depends_on(text)),
            "runner": runner or "", "runner_model": runner_model or "",
            "runners": sorted(dispatcher._REGISTERED_RUNNERS), "can_post_qa": open_pr,
            "suggestions": None, "spec_error": None, "spec_notice": None, **extra}


def register(app: FastAPI, shell: Shell, sessions_factory: Callable | None = None,
             suggest_run: Callable | None = None) -> None:
    home = shell.home

    def get_sessions():
        return sessions_factory() if sessions_factory else sessions.build_routing_sessions(shell.cfg())

    def need_confirm(f: dict) -> None:
        if f.get("confirm") != "1":
            raise Refused("trigger post-QA needs confirmation")

    def do_save(key: str, f: dict) -> dict:
        text = f.get("text", "").replace("\r\n", "\n").replace("\r", "\n")
        if f.get("hash") != (dispatcher.spec_hash_on_disk(home, key) or ""):
            raise _Kept("the spec changed since this editor loaded it; your text is kept below, "
                        "reload the tab and re-apply it", text)
        if not text.strip():
            raise _Kept("the spec must not be empty", text)
        store.atomic_write(store.spec_path(home, key), text)
        return {"spec_notice": "Spec saved."}

    def do_ac(key: str, f: dict) -> dict:
        ops.add_ac(shell.cfg(), key, f.get("text", ""))
        return {"spec_notice": "Acceptance criterion added."}

    def do_fields(key: str, f: dict) -> dict:
        raw_priority = f.get("priority", "").strip()
        try:
            priority = int(raw_priority) if raw_priority else None
        except ValueError:
            raise Refused(f"priority must be an integer >= 0, got {raw_priority!r}")
        deps = [d.strip() for d in f.get("depends_on", "").replace(",", " ").split() if d.strip()]
        ops.set_spec_fields(shell.cfg(), key, priority=priority, depends_on=deps)
        return {"spec_notice": "Priority and dependencies saved."}

    def do_runner(key: str, f: dict) -> dict:
        out = ops.set_runner(shell.cfg(), key, runner=f.get("runner", "").strip() or None,
                             runner_model=f.get("runner_model", "").strip() or None)
        return {"spec_notice": out.get("warning") or "Runner saved."}

    def do_suggest(key: str, f: dict) -> dict:
        kw = {"run": suggest_run} if suggest_run else {}
        return {"suggestions": ops.suggest_acs(shell.cfg(), key, **kw)}

    def do_suggest_add(key: str, f: dict) -> dict:
        n = int(f["count"]) if f.get("count", "").isdigit() else 0
        picked = [f.get(f"text-{i}", "") for i in range(n) if f.get(f"pick-{i}") == "1"]
        if not picked:
            raise Refused("tick at least one suggestion to add")
        for text in picked:
            ops.add_ac(shell.cfg(), key, text)
        return {"spec_notice": f"{len(picked)} acceptance criteri{'on' if len(picked) == 1 else 'a'} added."}

    def do_post_qa(key: str, f: dict) -> dict:
        need_confirm(f)
        if not spec_ctx(shell, key)["can_post_qa"]:
            raise Refused(f"{key}: no open pull request to run the post-QA skill on")
        r = dispatcher.trigger_post_qa_skill(shell.cfg(), get_sessions(), key)
        return {"spec_notice": f"post_qa_skill {r['skill']} fired (runner={r['runner']})."}

    actions = {"save": do_save, "ac": do_ac, "fields": do_fields, "runner": do_runner,
               "suggest": do_suggest, "suggest/add": do_suggest_add, "post-qa": do_post_qa}

    def run(request: Request, key: str, name: str, f: dict) -> HTMLResponse:
        extra: dict = {}
        error = None
        try:
            try:
                if not ticket_view.exists(home, key):
                    raise Refused(f"no such ticket: {key}")
            except store.MaestroError as e:
                raise Refused(str(e))
            extra = actions[name](key, f)
        except _Kept as e:
            error = {"form": name, "msg": str(e)}
            extra = {"editor_text": e.text}
        except store.MaestroError as e:
            error = {"form": name, "msg": str(e)}
            if name == "suggest/add":  # keep the proposals on screen so a retry needs no new model call
                n = int(f["count"]) if f.get("count", "").isdigit() else 0
                extra = {"suggestions": [f.get(f"text-{i}", "") for i in range(n)]}
        if error:
            ctx = {"spec_error": error, **extra}
        else:
            ctx = extra
        resp = shell.render(request, "_ticket_spec.html", **spec_ctx(shell, key, **ctx))
        if error:
            resp.status_code = 409
        return resp

    def make(name: str):
        async def handler(request: Request, key: str) -> HTMLResponse:
            f = _form(await request.body())
            return await run_in_threadpool(run, request, key, name, f)
        handler.__name__ = "spec_" + name.replace("/", "_").replace("-", "_")
        return handler

    for name in actions:
        shell.guard.post(app, f"/t/{{key}}/spec/{name}", make(name), response_class=HTMLResponse)


class _Kept(store.MaestroError):
    """A refused save that carries the typed text back into the editor."""

    def __init__(self, msg: str, text: str):
        super().__init__(msg)
        self.text = text
