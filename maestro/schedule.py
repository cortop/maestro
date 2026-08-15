"""Cadence for config-declared scheduled tasks: interval + cron due-checks.

Two cadence shapes share one seam (``is_due``/``next_due``/``dedup_bucket``): a
task with an ``every`` field keeps the original interval behavior (N seconds
elapsed since it last fired); a task with a ``cron`` field is matched against
wall-clock slots instead ("Monday 09:00", not "every 604800 seconds"). Exactly
one of the two fields is valid on any task -- enforced by ``ops.schedule_add``/
``schedule_edit`` and the TUI's ``_ScheduleModal``, not here.

Pure, stdlib-only, no filesystem/clock access -- the dispatcher supplies ``now``
and the persisted ``last_fired`` cursor, and every due-check is a function of its
arguments alone (proven by tests/test_schedule.py's ambient-``TZ``-independence
test). A cron task resolves wall-clock instants via ``datetime.fromtimestamp(now,
ZoneInfo(task.get("tz") or "UTC"))``, never ``time.localtime`` -- ``localtime``
reads ambient process/system timezone state (``TZ`` / ``/etc/localtime``), which
is not a function of its arguments and would break that purity. ``zoneinfo`` is
stdlib on 3.11+, so the core stays dependency-free. ``tz`` defaults to ``"UTC"``
(never has a DST transition, never depends on the host) precisely so a task that
never sets it can't hit either DST edge below; a human who wants "Monday 09:00 in
Paris" writes ``tz = "Europe/Paris"`` explicitly and gets exactly that -- the
rejected alternative, the machine's local wall clock, would make a laptop
crossing timezones (or a launchd job whose TZ differs from the human's shell)
silently move the board's schedule.

DST semantics, both boundaries (see ``cron_due_slot``):
- Fall-back (a local instant that occurs twice, e.g. ``America/New_York``
  2026-11-01 01:00): fires exactly once. A cron slot is identified by its NAIVE
  wall-clock fields, and Python's default ``fold=0`` always resolves the same
  naive instant to the same (first) real epoch, so there is only ever one
  candidate for "01:00 local" per calendar day -- the second occurrence is never
  even generated as a distinct candidate, let alone fired twice.
- Spring-forward (a local instant that does not exist, e.g. ``0 2 * * *`` the day
  the clock jumps 02:00 -> 03:00): fires at the next valid instant. Constructing
  the nonexistent naive local time and taking ``.timestamp()`` does not raise --
  it round-trips through the pre-transition UTC offset to the first REAL instant
  after the gap (03:00 local), which is exactly the epoch this module fires at.
  No exception is ever raised for this case; there is none to catch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_EVERY_RE = re.compile(r"^(\d+)([smhd])?$")

# Optional per-task fields that pass through into a scheduled task's minted ticket,
# beyond the five structural mint-args (intent/kind/priority/scheduled_by/dedup)
# that ``dispatcher.run_scheduled_tasks`` always sets. Shared
# with ``config._SCHEDULED_FIELDS`` so the mint-args allowlist and the config.toml
# round-trip allowlist can't drift apart. ``cron``/``tz`` are NOT here -- they
# configure the cadence itself, they never flow into the minted ticket.
OPTIONAL_MINT_FIELDS = ("repo", "model", "effort", "notes", "depends_on", "runner", "runner_model")

# Bound on how many days a cron search walks before giving up -- generous for
# any realistic daily/weekly/monthly cadence (a bit over a year), but a search
# is O(days-until-match) so a genuinely rare pattern (e.g. "only Feb 29") can
# still fail to find a match; that is an accepted, documented limitation rather
# than a real croniter-style engine, in exchange for staying stdlib-only.
_CRON_MAX_LOOKBACK_DAYS = 400
_CRON_MAX_LOOKAHEAD_DAYS = 400
_MINUTES_PER_DAY = 24 * 60


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
    """An interval task's cadence in seconds, from its 'every' field. Interval-only
    -- never called for a cron task (see ``dedup_bucket``/``run_scheduled_tasks``,
    which route a cron task behind ``cron_due_slot`` instead)."""
    return parse_every(task["every"])


def resolve_tz(tz: str | None) -> ZoneInfo:
    """The task's tz, defaulting to UTC. Raises ValueError with an actionable
    message for an unknown/unloadable zone -- callers (ops validation, the TUI
    modal) must surface this at write time, never let a task fire silently in
    UTC because its declared tz didn't load."""
    name = tz or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown timezone: {name!r}") from e


@dataclass(frozen=True)
class _CronSpec:
    minute: frozenset
    hour: frozenset
    dom: frozenset
    dom_star: bool
    month: frozenset
    dow: frozenset
    dow_star: bool


def _parse_cron_field(spec: str, low: int, high: int) -> frozenset:
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"invalid cron field: {spec!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise ValueError(f"invalid cron step: {spec!r}") from None
            if step <= 0:
                raise ValueError(f"invalid cron step: {spec!r}")
        if part == "*":
            lo, hi = low, high
        elif "-" in part:
            lo_s, hi_s = part.split("-", 1)
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                raise ValueError(f"invalid cron range: {spec!r}") from None
        else:
            try:
                lo = hi = int(part)
            except ValueError:
                raise ValueError(f"invalid cron field: {spec!r}") from None
        if lo > hi or lo < low or hi > high:
            raise ValueError(f"invalid cron field: {spec!r} (expected {low}-{high})")
        values.update(range(lo, hi + 1, step))
    if not values:
        raise ValueError(f"invalid cron field: {spec!r}")
    return frozenset(values)


def parse_cron(cron: str) -> _CronSpec:
    """Parse a standard 5-field cron expression (minute hour dom month dow) into
    a matchable spec. Supports ``*``, comma lists, ``a-b`` ranges, and ``*/n`` /
    ``a-b/n`` steps -- the common subset, not every vendor extension. Day-of-week
    is 0-7 with both 0 and 7 meaning Sunday (POSIX cron); day-of-month and
    day-of-week combine with OR (not AND) when both are restricted, per standard
    cron semantics. Raises ValueError with an actionable message on anything else."""
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(f"invalid cron expression (need 5 fields, minute hour dom month dow): {cron!r}")
    minute_s, hour_s, dom_s, month_s, dow_s = parts
    minute = _parse_cron_field(minute_s, 0, 59)
    hour = _parse_cron_field(hour_s, 0, 23)
    dom = _parse_cron_field(dom_s, 1, 31)
    month = _parse_cron_field(month_s, 1, 12)
    dow_raw = _parse_cron_field(dow_s, 0, 7)
    dow = frozenset(0 if d == 7 else d for d in dow_raw)
    return _CronSpec(minute=minute, hour=hour, dom=dom, dom_star=(dom_s.strip() == "*"),
                      month=month, dow=dow, dow_star=(dow_s.strip() == "*"))


def _date_matches(spec: _CronSpec, d: datetime) -> bool:
    if d.month not in spec.month:
        return False
    cron_dow = d.isoweekday() % 7  # Python Mon=1..Sun=7 -> cron Sun=0..Sat=6
    if spec.dom_star and spec.dow_star:
        return True
    if spec.dom_star:
        return cron_dow in spec.dow
    if spec.dow_star:
        return d.day in spec.dom
    return d.day in spec.dom or cron_dow in spec.dow


def _minute_matches(spec: _CronSpec, d: datetime) -> bool:
    return d.minute in spec.minute and d.hour in spec.hour


def cron_due_slot(task: dict, last_fired: float, now: float) -> float | None:
    """The latest wall-clock instant matching ``task['cron']`` (evaluated in
    ``task.get('tz')``, default UTC) that falls in the half-open window
    ``(last_fired, now]`` -- or ``None`` if nothing matched. Fires AT MOST ONE
    slot per call (the latest), never every slot that elapsed, mirroring the
    interval seam's "level-triggered, not edge-accumulating" behavior; this is
    also what makes the DST fall-back rule hold (see module docstring).

    Searches backward one calendar day at a time (cheap date-only check first),
    only walking a day's individual minutes once its date matches -- so a
    restrictive cron (few matching days) stays fast, and a daily/weekly one
    resolves within the first day or two, real-world sweep intervals notwith-
    standing. Once ANY candidate is found on the most-recent matching day, no
    earlier day can beat it (days move strictly backward in real time even
    though minutes within one DST-transition day briefly don't -- see
    ``_CRON_MAX_LOOKBACK_DAYS``), so the search stops there.
    """
    spec = parse_cron(task["cron"])
    tz = resolve_tz(task.get("tz"))
    floor = last_fired or 0
    if now <= floor:
        return None
    day = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(_CRON_MAX_LOOKBACK_DAYS):
        best = None
        if _date_matches(spec, day):
            for minute_of_day in range(_MINUTES_PER_DAY):
                probe = day + timedelta(minutes=minute_of_day)
                if not _minute_matches(spec, probe):
                    continue
                epoch = probe.timestamp()
                if floor < epoch <= now and (best is None or epoch > best):
                    best = epoch
        if best is not None:
            return best
        day -= timedelta(days=1)
    return None


def _cron_next_slot(task: dict, now: float) -> float:
    """The earliest wall-clock instant matching ``task['cron']`` strictly after
    ``now`` -- the forward-search mirror of ``cron_due_slot``, for display
    (``schedule_status`` / ``maestro schedule list``)."""
    spec = parse_cron(task["cron"])
    tz = resolve_tz(task.get("tz"))
    day = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(_CRON_MAX_LOOKAHEAD_DAYS):
        if _date_matches(spec, day):
            best = None
            for minute_of_day in range(_MINUTES_PER_DAY):
                probe = day + timedelta(minutes=minute_of_day)
                if not _minute_matches(spec, probe):
                    continue
                epoch = probe.timestamp()
                if epoch > now and (best is None or epoch < best):
                    best = epoch
            if best is not None:
                return best
        day += timedelta(days=1)
    raise ValueError(f"no upcoming match found for cron {task['cron']!r} within the lookahead bound")


def is_due(task: dict, last_fired: float, now: float) -> bool:
    """Has this task's cadence elapsed since last_fired? Dispatches on shape: a
    task carrying a 'cron' field is matched against wall-clock slots
    (``cron_due_slot``); otherwise it's an interval task and this is "has at
    least one full period elapsed since last_fired" -- ``last_fired`` of 0
    (never fired) is due as soon as one period has elapsed from the epoch, in
    practice immediately, since real epoch timestamps dwarf any cadence
    measured in seconds/minutes/hours/days.
    """
    if task.get("cron"):
        return cron_due_slot(task, last_fired, now) is not None
    return now - (last_fired or 0) >= period(task)


def next_due(task: dict, last_fired: float, now: float | None = None) -> float:
    """When this task next becomes due. Interval tasks: ``last_fired + period``,
    unchanged, ``now`` unused. Cron tasks: the next matching wall-clock slot
    strictly after ``now`` -- ``now`` is required (raises ValueError without it);
    ``schedule_status``, the sole caller with a cron task in scope, always has it."""
    if task.get("cron"):
        if now is None:
            raise ValueError("next_due: 'now' is required for a cron task")
        return _cron_next_slot(task, now)
    return (last_fired or 0) + period(task)


def advance_cursor(last_fired: float, now: float, period_seconds: int) -> float:
    """The cursor value to persist after an INTERVAL task fires at ``now``,
    anchored to the elapsed slot boundary rather than to the sweep clock.

    A never-fired task (``last_fired`` 0) has no prior anchor to derive a boundary
    from, so its first-ever fire anchors the cadence at ``now`` itself. Every fire
    after that snaps forward by whole periods from that anchor --
    ``last_fired + period * floor((now - last_fired) / period)`` -- so a late sweep
    advances the cursor to the most recent elapsed boundary, never past it, and a
    fire's lateness never compounds into the next fire's due time the way
    ``cursor = now`` would. Cron tasks don't use this -- ``run_scheduled_tasks``
    anchors a cron task's cursor to the exact matched slot from ``cron_due_slot``.
    """
    if not last_fired:
        return now
    elapsed_periods = int((now - last_fired) // period_seconds)
    return last_fired + period_seconds * elapsed_periods


def dedup_bucket(task: dict, now: float) -> str:
    """A stable token identifying "which cadence slot is this", closing the
    double-mint crash window the same way for both shapes (see
    ``dispatcher.run_scheduled_tasks`` and
    ``tests/test_dispatcher.py::test_mint_new_tickets_dedup_closes_cursor_crash_window``):
    a re-fire of the SAME slot (crash between minting the create-request and
    persisting the cursor) must produce the identical token, so the second fire
    dedups against the first instead of minting a duplicate ticket.

    Interval: the absolute period-count (``now // period``) -- unchanged.
    Cron: the matched slot's epoch-minute, found by searching backward from
    ``now`` with no lower bound (``last_fired=0``) -- deterministic given
    ``(task, now)` alone, so two fires inside the same slot always agree on it
    regardless of what their cursors happened to read.
    """
    if task.get("cron"):
        slot = cron_due_slot(task, 0, now)
        if slot is None:
            # Only reachable if a caller invokes this without having first
            # confirmed is_due() -- stay total rather than raise mid-sweep.
            slot = now
        return str(int(slot // 60))
    return str(int(now // period(task)))
