"""Fleet/pulse view-model -- the read-only counters behind the TUI pulse strip."""
from __future__ import annotations

from pathlib import Path

from .. import health, ratelimit, spend as spend_mod, store
from ..config import Config

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

