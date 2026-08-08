"""Cadence for config-declared scheduled tasks: interval parsing and due-checks.

Interval-only for now (30m/6h/24h/seconds); a cron matcher is a clean follow-up
behind the same ``is_due`` seam. Pure, stdlib-only, no filesystem/clock access —
the dispatcher supplies ``now`` and the persisted ``last_fired`` cursor.
"""
from __future__ import annotations

import re

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_EVERY_RE = re.compile(r"^(\d+)([smhd])?$")

# Optional per-task fields that pass through into a scheduled task's minted ticket,
# beyond the six structural mint-args (intent/kind/approval_tier/priority/
# scheduled_by/dedup) that ``dispatcher.run_scheduled_tasks`` always sets. Shared
# with ``config._SCHEDULED_FIELDS`` so the mint-args allowlist and the config.toml
# round-trip allowlist can't drift apart.
OPTIONAL_MINT_FIELDS = ("repo", "model", "effort", "notes", "depends_on")


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


def advance_cursor(last_fired: float, now: float, period_seconds: int) -> float:
    """The cursor value to persist after a fire at ``now``, anchored to the elapsed
    slot boundary rather than to the sweep clock.

    A never-fired task (``last_fired`` 0) has no prior anchor to derive a boundary
    from, so its first-ever fire anchors the cadence at ``now`` itself. Every fire
    after that snaps forward by whole periods from that anchor --
    ``last_fired + period * floor((now - last_fired) / period)`` -- so a late sweep
    advances the cursor to the most recent elapsed boundary, never past it, and a
    fire's lateness never compounds into the next fire's due time the way
    ``cursor = now`` would.
    """
    if not last_fired:
        return now
    elapsed_periods = int((now - last_fired) // period_seconds)
    return last_fired + period_seconds * elapsed_periods
