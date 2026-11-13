"""Fleet page routes of `maestro web`: the fleet health picture and its guarded actions."""
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .. import backup, fleet, human_actions, spend as spend_mod, store
from ..view import fleet as fleet_view
from .shell import Shell

log = logging.getLogger(__name__)


def _safe(label: str, fn, *args):
    """Run one view-model read; a failure degrades that panel to ``None`` ("unknown")."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 -- a broken panel must never turn the page into a 500
        log.warning("fleet panel %s unreadable", label, exc_info=True)
        return None


def _spend_panel(home, pulse: dict | None) -> dict:
    """Spend against ceiling, or ``known: False`` when the meter is unavailable or unreadable."""
    sp = (pulse or {}).get("spend") or {}
    today, ceiling = sp.get("today_usd"), sp.get("ceiling_usd")
    corrupt = (spend_mod._state_path(home).exists()
               and not isinstance(_safe("spend-state", store.read_json,
                                        spend_mod._state_path(home), None), dict))
    known = pulse is not None and not sp.get("unavailable") and today is not None and not corrupt
    fracs = sorted(f for f in (pulse or {}).get("warn_fractions", []) if 0 < f <= 1)
    pct = min(100.0, today / ceiling * 100) if known and ceiling else 0.0
    return {"known": known, "today": today, "ceiling": ceiling, "pct": round(pct, 1),
            "marks": [round(f * 100, 1) for f in fracs] if ceiling else [],
            "over": bool(known and ceiling and today >= ceiling)}


def fleet_ctx(shell: Shell) -> dict:
    now = store.now_epoch()
    home, cfg = shell.home, shell.cfg()
    pulse = _safe("pulse", fleet_view.pulse, home, cfg, now)
    hb = (pulse or {}).get("heartbeat") or {}
    doctor = _safe("doctor", fleet_view.doctor_rows, cfg, now)
    problems = [r for r in doctor or [] if r.get("status") in ("fail", "warn")]
    passing = [r for r in doctor or [] if r.get("status") not in ("fail", "warn")]
    paused = _safe("pause", fleet.pause_state, home, now)
    buckets = (pulse or {}).get("buckets") or []
    top = max(buckets, default=0)
    spend = _spend_panel(home, pulse)

    attention = []
    if pulse is None or not hb:
        attention.append("heartbeat unknown: no dispatcher sweep recorded")
    if pulse and pulse.get("runaway"):
        attention.append(f"spawn rate over budget ({pulse['spawns']}/{pulse['budget']} in the last hour)")
    if spend["over"]:
        attention.append("daily spend ceiling reached")
    if not spend["known"]:
        attention.append("spend meter unknown")
    if doctor is None:
        attention.append("doctor unavailable")
    if problems:
        attention.append(f"{len(problems)} doctor check(s) need attention")
    if paused is not None:
        state = "paused"
    elif pulse and pulse.get("paused_until") is not None:
        state = "rate-limited"
    elif pulse is None or not hb:
        state = "unknown"
    elif attention:
        state = "needs attention"
    else:
        state = "healthy"
    return {
        "state": state, "attention": attention, "pulse": pulse, "hb": hb, "spend": spend,
        "buckets": [{"n": n, "pct": round(n / top * 100) if top else 0} for n in buckets],
        "paused": paused, "now": now,
        "sessions": _safe("sessions", fleet_view.session_rows, home, cfg, now),
        "schedule": _safe("schedule", fleet_view.schedule_rows, cfg, now),
        "backups": _safe("backups", fleet_view.backup_panel, cfg, now),
        "slack": _safe("slack", fleet_view.slack_panel, cfg, now),
        "doctor": doctor, "problems": problems, "passing": passing,
    }


async def _form(request: Request) -> dict[str, str]:
    """First value of each field of an urlencoded body (no multipart dependency)."""
    raw = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _parse_until(value: str) -> float:
    """A bare epoch or an ISO-8601 string (naive = local time); raises ``ValueError``."""
    try:
        return float(value)
    except ValueError:
        return datetime.fromisoformat(value).timestamp()


def register(app: FastAPI, shell: Shell) -> None:
    @app.get("/fleet", response_class=HTMLResponse)
    def fleet_page(request: Request):
        return shell.render(request, "fleet.html", nav="fleet", **fleet_ctx(shell))

    @app.get("/fleet/panels", response_class=HTMLResponse)
    def fleet_panels(request: Request):
        return shell.render(request, "_fleet_panels.html", nav="fleet", **fleet_ctx(shell))

    def panels(request: Request, **extra):
        return shell.render(request, "_fleet_panels.html", nav="fleet", **fleet_ctx(shell), **extra)

    async def pause(request: Request):
        form = await _form(request)
        until_raw = form.get("until", "").strip()
        try:
            until = _parse_until(until_raw) if until_raw else None
        except ValueError:
            return panels(request, notice=f"invalid until time {until_raw!r}")
        human_actions.pause(shell.home, until=until, reason=form.get("reason", "").strip() or None)
        return panels(request, notice="fleet paused")

    async def resume(request: Request):
        human_actions.resume(shell.home)
        return panels(request, notice="fleet resumed")

    async def doctor(request: Request):
        return panels(request, notice="doctor re-run")

    async def backup_now(request: Request):
        try:
            path = backup.create_backup(shell.cfg(), store.now_epoch())
        except (OSError, store.MaestroError) as e:
            return panels(request, notice=f"backup failed: {e}")
        return panels(request, notice=f"backup created: {path.name}")

    @app.get("/fleet/stop/{key}", response_class=HTMLResponse)
    def stop_confirm(request: Request, key: str):
        return shell.render(request, "_fleet_stop_confirm.html", nav="fleet", key=key)

    async def stop(request: Request, key: str):
        if (await _form(request)).get("confirm") != "yes":
            return panels(request, notice=f"stop {key} not confirmed")
        try:
            human_actions.stop(shell.cfg(), key)
        except (human_actions.StopRefused, store.MaestroError) as e:
            return panels(request, notice=f"stop {key} refused: {e}")
        return panels(request, notice=f"stopped {key}")

    guard = shell.guard
    guard.post(app, "/fleet/pause", pause, response_class=HTMLResponse)
    guard.post(app, "/fleet/resume", resume, response_class=HTMLResponse)
    guard.post(app, "/fleet/doctor", doctor, response_class=HTMLResponse)
    guard.post(app, "/fleet/backup", backup_now, response_class=HTMLResponse)
    guard.post(app, "/fleet/stop/{key}", stop, response_class=HTMLResponse)
