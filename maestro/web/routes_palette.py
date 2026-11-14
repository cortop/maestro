"""Command palette fragment of `maestro web`: the entries valid right now, grouped for the script."""
from __future__ import annotations

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse

from .. import fleet, projection, store
from ..view import board as board_view
from ..view import ticket as ticket_view
from . import routes_spec, routes_ticket
from .shell import Shell

MAX_TICKETS = 300


def _go(label: str, href: str, hint: str = "", keys: str = "") -> dict:
    return {"label": label, "href": href, "hint": hint, "tui": keys, "text": f"{label} {keys}"}


def _post(label: str, url: str, keys: str = "") -> dict:
    return {"label": label, "post": url, "tui": keys, "hint": "", "text": label}


def ticket_entries(shell: Shell, key: str) -> list[dict]:
    """Actions of *key* valid for its phase; each carries the TUI key for the same action."""
    ctx = routes_ticket.head_ctx(shell, key)
    snap, write = ctx["t"].snap, shell.guard.writes_enabled
    out: list[dict] = []
    if write and (snap.open_questions or snap.question_open):
        out.append(_go(f"Answer {key}", "/", "decision cards", "a"))
    if write and ctx["can_message"]:
        out.append(_go(f"Message agent on {key}", f"/t/{key}", "message form", "i"))
    if write and snap.phase == "degraded":
        out.append(_post(f"Retry {key}", f"/t/{key}/retry", "ctrl+r"))
        out.append(_go(f"Discard {key}", f"/t/{key}", "confirm on the ticket", "ctrl+d"))
    if write and snap.kind == "review" and snap.phase == "awaiting-human" and snap.review_brief:
        out.append(_go(f"Create pending review for {key}", f"/t/{key}?tab=review",
                       "edit, then create on the Review tab"))
    if write:
        spec = f"/t/{key}?tab=spec"
        out.append(_go(f"Edit spec of {key}", spec, "editor on the Spec tab", "E"))
        out.append(_go(f"Add acceptance criterion to {key}", spec, "add-AC form", "A"))
        out.append(_go(f"Suggest acceptance criteria for {key}", spec, "suggest on the Spec tab", "g"))
        out.append(_go(f"Set priority or dependencies of {key}", spec, "form on the Spec tab", "M"))
        out.append(_go(f"Set runner of {key}", spec, "form on the Spec tab", "o"))
        if routes_spec.spec_ctx(shell, key)["can_post_qa"]:
            out.append(_go(f"Trigger post-QA for {key}", spec, "confirm on the Spec tab", "Q"))
    if write and ctx["can_hold"]:
        out.append(_post(f"Hold {key}", f"/t/{key}/hold", "h"))
    if write and ctx["can_release"]:
        out.append(_post(f"Release {key}", f"/t/{key}/release", "z"))
    if write and ctx["can_stop"]:
        out.append(_go(f"Stop session of {key}", f"/t/{key}", "confirm on the ticket", "K"))
    return out


def goto_entries(shell: Shell, current: str | None) -> list[dict]:
    home = shell.home
    out = [_go("Go to needs you", "/tickets?filter=needs-you", keys="f")]
    running = sorted(board_view.running_keys(home, shell.cfg()) - {current})
    if running:
        out.append(_go(f"Go to next running ticket ({running[0]})", f"/t/{running[0]}", keys="j"))
    snaps = sorted(projection.load_snapshots(home), key=lambda s: s.key)[:MAX_TICKETS]
    for s in snaps:
        out.append(_go(f"{s.key} {s.title}", f"/t/{s.key}", s.phase))
    return out


def fleet_entries(shell: Shell) -> list[dict]:
    out = [_go("Open fleet", "/fleet", keys="3")]
    if not shell.guard.writes_enabled:
        return out
    if fleet.pause_state(shell.home, store.now_epoch()) is None:
        out.append(_post("Pause fleet", "/fleet/pause", "P"))
    else:
        out.append(_post("Resume fleet", "/fleet/resume", "P"))
    out.append(_post("Re-run doctor", "/fleet/doctor"))
    out.append(_post("Back up now", "/fleet/backup"))
    return out


def register(app: FastAPI, shell: Shell) -> None:
    @app.get("/palette", response_class=HTMLResponse)
    def palette(request: Request, key: str | None = Query(None)):
        try:
            on_ticket = bool(key) and ticket_view.exists(shell.home, key)
        except store.MaestroError:
            on_ticket = False
        groups = []
        if on_ticket:
            groups.append((f"{key} actions", ticket_entries(shell, key)))
        groups.append(("Go to", goto_entries(shell, key if on_ticket else None)))
        groups.append(("Fleet", fleet_entries(shell)))
        return shell.render(request, "_palette.html", groups=[g for g in groups if g[1]])
