"""Fleet page routes (`/fleet`, `/fleet/panels`) of `maestro web`: a read-only fleet health picture."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .. import fleet, spend as spend_mod, store
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


def register(app: FastAPI, shell: Shell) -> None:
    @app.get("/fleet", response_class=HTMLResponse)
    def fleet_page(request: Request):
        return shell.render(request, "fleet.html", nav="fleet", **fleet_ctx(shell))

    @app.get("/fleet/panels", response_class=HTMLResponse)
    def fleet_panels(request: Request):
        return shell.render(request, "_fleet_panels.html", nav="fleet", **fleet_ctx(shell))
