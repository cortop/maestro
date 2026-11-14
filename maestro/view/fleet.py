"""Fleet/pulse view-model -- the read-only counters behind the TUI pulse strip."""
from __future__ import annotations

from pathlib import Path

from .. import (backup, claims, dispatcher, health, ratelimit, sessions, slack_watcher,
                spend as spend_mod, steplog, store)
from ..config import Config
from . import rounds as rounds_view

_SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[int]) -> str:
    top = max(values, default=0)
    if top <= 0:
        return _SPARK[0] * len(values)
    return "".join(_SPARK[min(len(_SPARK) - 1, round(v / top * (len(_SPARK) - 1)))]
                   for v in values)


def pulse(home: Path, cfg: Config, now: float | None = None) -> dict:
    """Spawn rate, budget, spend and heartbeat counters; reads only, no probe."""
    now = store.now_epoch() if now is None else now
    rate = health.spawn_rate(home, now)
    budget = health.spawn_budget(cfg)
    hb = store.read_json(store.heartbeat_path(home), {})
    rl = ratelimit.status(home, now)
    return {
        "buckets": health.pulse_buckets(home, now),
        "spawns": rate["total"],
        "by_key": rate["by_key"],
        "budget": budget,
        "runaway": bool(budget) and rate["total"] > budget,
        "spend": spend_mod.status(cfg, now),
        "warn_fractions": list(cfg.alarm_spend_warn_fractions),
        "heartbeat": hb if isinstance(hb, dict) else {},
        "paused_until": rl.get("paused_until") if rl.get("paused") else None,
    }



def session_rows(home: Path, cfg: Config, now: float | None = None) -> list[dict]:
    """One row per live claim: key, round label, live tool line, runner, pid, age and the
    session's cost. ``cost`` is ``None`` (absent, never zero) until its log carries a result."""
    now = store.now_epoch() if now is None else now
    rows = []
    for c in claims.describe_claims(home, max_age=cfg.unverified_claim_max_age):
        if not c["claimed"]:
            continue
        key = c["key"]
        row = {"key": key, "round": "", "tool": "", "runner": None, "pid": c["pid"],
               "age_s": c["age_s"], "cost": None}
        try:
            row["round"] = rounds_view.round_label(home, key)
            logs = sessions.list_sessions(home, key)
        except (OSError, ValueError, store.MaestroError):
            logs = []
        if logs:
            path = Path(logs[0]["path"])
            row["runner"] = steplog.session_model(path)["runner"]
            status, _ = steplog.live_status(path)
            row["tool"] = steplog.live_texts(status, now)[1]
            if logs[0]["format"] == "stream-json":
                row["cost"] = steplog.result_summary(
                    obj for _o, obj in steplog.iter_records(path))["cost"]
        rows.append(row)
    return rows


def doctor_rows(cfg: Config, now: float | None = None) -> list[dict]:
    """`health.run_checks` results, problems (fail, then warn) before ok."""
    now = store.now_epoch() if now is None else now
    order = {"fail": 0, "warn": 1}
    return sorted(health.run_checks(cfg, now), key=lambda r: order.get(r.get("status"), 2))


def backup_panel(cfg: Config, now: float | None = None) -> dict:
    """Last backup epoch, next due epoch (``None`` when disabled or none taken yet),
    count and the interval/retention knobs."""
    last = backup.newest_backup_epoch(cfg)
    interval = cfg.backup_interval or 0
    return {"last": last, "next": last + interval if last is not None and interval > 0 else None,
            "count": len(backup.list_backups(cfg)), "interval": interval,
            "retention": cfg.backup_retention}


def backup_files(cfg: Config) -> list[dict]:
    """Each backup tarball, newest first: name, filename-derived epoch and size in bytes."""
    rows = []
    for path in reversed(backup.list_backups(cfg)):
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        rows.append({"name": path.name, "epoch": backup._epoch_from_name(path.name), "size": size})
    return rows


def claim_rows(cfg: Config) -> list[dict]:
    """Each claim with key, pid, age and verdict (``claims.describe_claims``)."""
    return claims.describe_claims(cfg.home, max_age=cfg.unverified_claim_max_age)


def schedule_rows(cfg: Config, now: float | None = None) -> list[dict]:
    """Each scheduled task with its last-fired and next-due epochs (``schedule.next_due``)."""
    now = store.now_epoch() if now is None else now
    return dispatcher.schedule_status(cfg, now)


def slack_panel(cfg: Config, now: float | None = None) -> dict:
    """The Slack watcher's last poll epoch (``None`` before the first), polls today against
    the daily cap, and its spend today (``slack_watcher.panel``)."""
    now = store.now_epoch() if now is None else now
    out = slack_watcher.panel(cfg, now)
    if out["last_poll"] is None:
        out["last_poll"] = out["last_spawn"]
    return out
