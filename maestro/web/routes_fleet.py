"""Fleet page routes of `maestro web`: the fleet health picture and its guarded actions."""
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from .. import backup, dispatcher, fleet, human_actions, ops, projection, ratelimit, spend as spend_mod, store
from .. import claims as claims_mod
from ..sessions import DryRunSessions, build_routing_sessions
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
        "backup_files": _safe("backup-files", fleet_view.backup_files, cfg),
        "claims": _safe("claims", fleet_view.claim_rows, cfg),
        "launchd": _safe("launchd", fleet.status, home),
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


def _launchd_notice(action: str, out: dict) -> str:
    """One line for a `fleet up` / `fleet down` result: the exit code and launchd's output."""
    detail = out.get("stdout") or ""
    return f"fleet {action}: exit {out.get('rc')}" + (f" · {detail}" if detail else "")


def register(app: FastAPI, shell: Shell, sessions_factory=None) -> None:
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

    def sched_err(request: Request, form: str, name: str, e: Exception, values: dict | None = None):
        """Re-render the panels with *e* shown inline on schedule form *form*; nothing was written."""
        return panels(request, schedule_error={"form": form, "name": name, "msg": str(e),
                                               "fields": values or {}})

    def _task_fields(form: dict[str, str]) -> dict:
        """The editable text fields of a task form; blank ones become ``None``."""
        return {f: (form.get(f, "").strip() or None) for f in ("prompt", "every", "cron", "tz")}

    async def schedule_add(request: Request):
        form = await _form(request)
        name = form.get("name", "").strip()
        task = {"name": name, **_task_fields(form), "kind": "implementation",
                "priority": 3, "enabled": True}
        try:
            ops.schedule_add(shell.cfg(), task)
        except store.MaestroError as e:
            return sched_err(request, "add", "", e, form)
        return panels(request, notice=f"scheduled task {name} added")

    async def schedule_edit(request: Request):
        form = await _form(request)
        name = form.get("name", "").strip()
        fields = _task_fields(form)
        try:
            if not fields["prompt"]:
                raise store.MaestroError("schedule edit: 'prompt' is required")
            ops.schedule_edit(shell.cfg(), name, fields)
        except store.MaestroError as e:
            return sched_err(request, "edit", name, e, form)
        return panels(request, notice=f"scheduled task {name} updated")

    async def schedule_remove(request: Request):
        name = (await _form(request)).get("name", "").strip()
        try:
            ops.schedule_remove(shell.cfg(), name)
        except store.MaestroError as e:
            return sched_err(request, "remove", name, e)
        return panels(request, notice=f"scheduled task {name} removed")

    def _toggle(enabled: bool):
        async def handler(request: Request):
            name = (await _form(request)).get("name", "").strip()
            try:
                ops.schedule_set_enabled(shell.cfg(), name, enabled)
            except store.MaestroError as e:
                return sched_err(request, "toggle", name, e)
            return panels(request, notice=f"scheduled task {name} {'enabled' if enabled else 'disabled'}")
        return handler

    def _list(label: str, keys) -> str:
        return f"{label} {', '.join(keys)}" if keys else f"{label} nothing"

    async def fleet_up(request: Request):
        if (await _form(request)).get("confirm") != "yes":
            return panels(request, notice="fleet up not confirmed")
        out = await run_in_threadpool(fleet.up, shell.home, cfg=shell.cfg())
        return panels(request, notice=_launchd_notice("up", out))

    async def fleet_down(request: Request):
        if (await _form(request)).get("confirm") != "yes":
            return panels(request, notice="fleet down not confirmed")
        out = await run_in_threadpool(fleet.down, shell.home)
        return panels(request, notice=_launchd_notice("down", out))

    def _sweep(dry_run: bool) -> str:
        cfg = shell.cfg()
        if dry_run:
            sessions = DryRunSessions()
        else:
            sessions = sessions_factory() if sessions_factory else build_routing_sessions(cfg)
        report = dispatcher.dispatch(cfg, sessions, store.now_epoch(), dry_run=dry_run)
        if not dry_run:
            projection.write(cfg.home)
        if dry_run:
            return ("dry run: " + _list("would mint", report.would_mint) + "; "
                    + _list("would spawn", report.spawned))
        return "sweep: " + _list("spawned", report.spawned) + "; " + _list("minted", report.minted)

    async def sweep(request: Request):
        try:
            return panels(request, notice=await run_in_threadpool(_sweep, False))
        except store.MaestroError as e:
            return panels(request, notice=f"sweep failed: {e}")

    async def dry_run(request: Request):
        try:
            return panels(request, notice=await run_in_threadpool(_sweep, True))
        except store.MaestroError as e:
            return panels(request, notice=f"dry run failed: {e}")

    async def clear_rate_limit(request: Request):
        if ratelimit.paused_until(shell.home, store.now_epoch()) is None:
            return panels(request, notice="fleet is not rate-limited")
        ratelimit.clear(shell.home)
        return panels(request, notice="rate limit cleared")

    async def purge_claims(request: Request):
        cfg = shell.cfg()
        rows = claims_mod.describe_claims(cfg.home, max_age=cfg.unverified_claim_max_age)
        dropped = [r["key"] for r in rows if not r["claimed"]]
        for key in dropped:
            claims_mod.release(cfg.home, key)
        return panels(request, notice=_list("purged stale claims:", dropped))

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
    guard.post(app, "/fleet/schedule/add", schedule_add, response_class=HTMLResponse)
    guard.post(app, "/fleet/schedule/edit", schedule_edit, response_class=HTMLResponse)
    guard.post(app, "/fleet/schedule/remove", schedule_remove, response_class=HTMLResponse)
    guard.post(app, "/fleet/schedule/enable", _toggle(True), response_class=HTMLResponse)
    guard.post(app, "/fleet/schedule/disable", _toggle(False), response_class=HTMLResponse)
    guard.post(app, "/fleet/up", fleet_up, response_class=HTMLResponse)
    guard.post(app, "/fleet/down", fleet_down, response_class=HTMLResponse)
    guard.post(app, "/fleet/sweep", sweep, response_class=HTMLResponse)
    guard.post(app, "/fleet/dry-run", dry_run, response_class=HTMLResponse)
    guard.post(app, "/fleet/clear-rate-limit", clear_rate_limit, response_class=HTMLResponse)
    guard.post(app, "/fleet/purge-claims", purge_claims, response_class=HTMLResponse)
