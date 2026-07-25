"""Fleet-wide health: liveness (heartbeat, dead-letters) plus a spawn-rate budget
that trips ``runaway`` when observed spawns/hour exceed what the rate guards
(``dispatcher.spawn_floor``) themselves permit. The existing liveness signals only
answer "is the dispatcher alive"; this answers "is it doing too much" (the
2026-07-19 incident: fresh heartbeat, zero dead letters, 21,731 no-op spawns).
"""
from __future__ import annotations

import math
from pathlib import Path

from . import dispatcher, store
from .config import Config

WINDOW_SECONDS = 3600


def spawn_rate(home: Path, now: float, window: int = WINDOW_SECONDS) -> dict:
    """Spawns actually observed in the trailing *window* seconds, from the ledger.

    The ledger is rewritten only when something spawns, so ``recent`` can be
    stale during a quiet stretch -- always filter by window here rather than
    trusting the file to be pre-trimmed. Legacy bare-float entries (pre-history
    ledgers) carry no history and contribute zero to the rate.
    """
    ledger = store.read_json(dispatcher._spawn_ledger_path(home), {}) or {}
    by_key: dict[str, int] = {}
    for key, entry in ledger.items():
        recent = entry.get("recent", []) if isinstance(entry, dict) else []
        count = sum(1 for t in recent if isinstance(t, (int, float)) and now - t <= window)
        if count:
            by_key[key] = count
    return {"total": sum(by_key.values()), "by_key": by_key}


def spawn_budget(cfg: Config) -> int:
    """Fleet-wide spawns/hour the rate guards themselves permit, unless overridden
    by the ``runaway_spawns_per_hour`` knob (0 disables the runaway check).

    Default: the per-key allowance ``ceil(3600 / effective_floor)`` -- exactly
    what ``spawn_floor`` permits one key -- times the number of tickets. This
    scales with board size and, critically, is not silenced by the same
    ``min_spawn_interval = 0`` misconfiguration the detector exists to catch:
    ``spawn_floor`` legitimately returns 0, so fall back to a sane per-key floor
    instead of dividing by zero.
    """
    if cfg.runaway_spawns_per_hour is not None:
        return int(cfg.runaway_spawns_per_hour)
    effective_floor = dispatcher.spawn_floor(cfg) or max(cfg.reconcile_steady_interval, 60)
    n_keys = len(dispatcher.list_keys(cfg.home))
    return n_keys * math.ceil(3600 / effective_floor)


def report(cfg: Config, now: float) -> dict:
    """The full doctor payload. ``cmd_doctor`` and the TUI fleet view both render
    this exact dict, so there is only one implementation of "is this too much"."""
    home = cfg.home
    hb = store.read_json(home / "derived" / ".heartbeat.json", {})
    age = round(now - hb["epoch"]) if hb.get("epoch") else None
    dl_dir = home / "tickets" / "_deadletter"
    dead = list(dl_dir.glob("*.md")) if dl_dir.exists() else []
    rate = spawn_rate(home, now)
    budget = spawn_budget(cfg)
    return {
        "heartbeat": hb,
        "heartbeat_age_s": age,
        "dead_letters": [p.stem for p in dead],
        "stale": age is not None and age > 1800,
        "spawns_last_hour": rate,
        "throttled_last_sweep": hb.get("throttled", 0),
        "spawn_budget_per_hour": budget,
        "runaway": bool(budget) and rate["total"] > budget,
    }
