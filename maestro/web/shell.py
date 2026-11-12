"""Shared page shell for `maestro web`: template rendering plus the header's fleet state and spend."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import fleet, ratelimit, spend, store
from ..config import Config
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
        """Fleet state and today's spend against the ceiling, for the header."""
        now = store.now_epoch()
        cfg = self.cfg()
        if fleet.pause_state(self.home, now) is not None:
            state = "paused"
        elif ratelimit.status(self.home, now).get("paused"):
            state = "rate-limited"
        else:
            state = "live"
        sp = spend.status(cfg, now)
        return {"state": state, "spend": sp["today_usd"], "ceiling": sp["ceiling_usd"],
                "spend_unavailable": sp["unavailable"]}

    def render(self, request: Request, name: str, *, nav: str = "now", **ctx) -> HTMLResponse:
        ctx = {"home": str(self.home.resolve()), "nav": nav, "nav_items": NAV,
               "shell_status": self.status(),
               "can_write": self.guard.writes_enabled, "write_token": self.guard.token, **ctx}
        return self.templates.TemplateResponse(request, name, ctx)
