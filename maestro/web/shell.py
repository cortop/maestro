"""Shared page shell for `maestro web`: template rendering plus the header's fleet state, counts and spend."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import fleet, projection, ratelimit, spend, store
from ..config import Config
from ..view import board as board_view
from .guard import WriteGuard

NAV = (("now", "Now", "/"), ("tickets", "Tickets", "/tickets"), ("fleet", "Fleet", "/fleet"))


class Shell:
    """Holds the home, config loader and templates every route module renders through."""

    def __init__(self, home: Path, load_cfg: Callable[[Path], Config], templates: Jinja2Templates,
                 guard: WriteGuard | None = None):
        self.home = home
        self.load_cfg = load_cfg
        self.templates = templates
        self.guard = guard or WriteGuard()

    def cfg(self) -> Config:
        return self.load_cfg(self.home)

    def status(self) -> dict:
        """Fleet state, live sessions, ticket counts and today's spend against the ceiling, for the header."""
        now = store.now_epoch()
        cfg = self.cfg()
        if fleet.pause_state(self.home, now) is not None:
            state = "paused"
        elif ratelimit.status(self.home, now).get("paused"):
            state = "rate-limited"
        else:
            state = "live"
        sp = spend.status(cfg, now)
        snaps = projection.load_snapshots(self.home)
        return {"state": state, "spend": sp["today_usd"], "ceiling": sp["ceiling_usd"],
                "spend_unavailable": sp["unavailable"],
                "sessions": len(board_view.running_keys(self.home, cfg)), "tickets": len(snaps),
                "needs": sum(1 for s in snaps if board_view.needs_you(self.home, s))}

    def render(self, request: Request, name: str, *, nav: str = "now", **ctx) -> HTMLResponse:
        """Render *name*. A full page (and the header's own fragment) gets the header status and
        nav badges; any other ``_fragment`` skips that work."""
        if not name.startswith("_") or name == "_shell_status.html":
            st = self.status()
            needs = st["needs"]
            ctx = {"shell_status": st,
                   "nav_badges": {
                       "now": {"count": needs, "alert": needs > 0,
                               "sub": f"{needs} need you" if needs != 1 else "1 needs you"},
                       "tickets": {"count": st["tickets"], "alert": False, "sub": f"{st['tickets']} tickets"},
                       "fleet": {"count": 0, "alert": False, "sub": st["state"]}},
                   **ctx}
        ctx = {"home": str(self.home.resolve()), "nav": nav, "nav_items": NAV,
               "can_write": self.guard.writes_enabled, "write_token": self.guard.token, **ctx}
        return self.templates.TemplateResponse(request, name, ctx)
