"""Cadence for config-declared scheduled tasks: interval parsing and due-checks.

Interval-only for now (30m/6h/24h/seconds); a cron matcher is a clean follow-up
behind the same ``is_due`` seam. Pure, stdlib-only, no filesystem/clock access —
the dispatcher supplies ``now`` and the persisted ``last_fired`` cursor.
"""
from __future__ import annotations

import re

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_EVERY_RE = re.compile(r"^(\d+)([smhd])?$")


def parse_every(s) -> int:
    """Parse an interval spec ("30m", "6h", "24h", "90", or a bare int) into seconds."""
    if isinstance(s, bool):
        raise ValueError(f"invalid 'every' interval: {s!r}")
    if isinstance(s, (int, float)):
        return int(s)
    m = _EVERY_RE.match(str(s).strip())
    if not m:
        raise ValueError(f"invalid 'every' interval: {s!r}")
    value = int(m.group(1))
    unit = m.group(2) or "s"
    return value * _UNIT_SECONDS[unit]


def period(task: dict) -> int:
    """A task's cadence in seconds, from its 'every' field."""
    return parse_every(task["every"])


def is_due(task: dict, last_fired: float, now: float) -> bool:
    """Has at least one full period elapsed since last_fired?

    ``last_fired`` of 0 (never fired) is due as soon as one period has elapsed
    from the epoch — in practice, immediately, since real epoch timestamps
    dwarf any cadence measured in seconds/minutes/hours/days.
    """
    return now - (last_fired or 0) >= period(task)


def next_due(task: dict, last_fired: float) -> float:
    """When this task next becomes due, given its last fire time (0 = never fired)."""
    return (last_fired or 0) + period(task)
