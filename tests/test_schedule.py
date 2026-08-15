"""Unit tests for maestro/schedule.py (cadence parsing + due-checks) and the
config.py TOML read/write round-trip for `[[scheduled]]` tasks."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from maestro import config as config_mod, schedule, store


# --- parse_every -------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("30m", 1800),
    ("6h", 21600),
    ("24h", 86400),
    ("90", 90),
    ("90s", 90),
    ("1d", 86400),
    (120, 120),
    (120.0, 120),
])
def test_parse_every(raw, expected):
    assert schedule.parse_every(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "30x", "-5m", None, True])
def test_parse_every_rejects_invalid(raw):
    with pytest.raises((ValueError, TypeError, AttributeError)):
        schedule.parse_every(raw)


def test_period_reads_every_field():
    assert schedule.period({"every": "1h"}) == 3600


# --- is_due / next_due --------------------------------------------------------

def test_is_due_never_fired_at_realistic_epoch():
    """last_fired=0 (never fired) is due as soon as a real epoch `now` is checked —
    epoch time dwarfs any cadence, so a fresh task fires on its very first sweep."""
    task = {"every": "1h"}
    assert schedule.is_due(task, 0, now=1_000_000)


def test_is_due_never_fired_not_due_before_one_period_from_epoch():
    task = {"every": "1h"}
    assert not schedule.is_due(task, 0, now=3599)


def test_is_due_false_before_interval():
    task = {"every": "1h"}
    assert not schedule.is_due(task, last_fired=1000, now=1000 + 3599)


def test_is_due_true_at_interval_boundary():
    task = {"every": "1h"}
    assert schedule.is_due(task, last_fired=1000, now=1000 + 3600)


def test_next_due():
    task = {"every": "1h"}
    assert schedule.next_due(task, 1000) == 4600
    assert schedule.next_due(task, 0) == 3600


# --- advance_cursor (GA-9: anchor the cadence cursor, no drift) ---------------

def test_advance_cursor_never_fired_anchors_to_now():
    """A never-fired task (cursor 0) has no prior boundary -- its first fire
    anchors the cadence at `now` itself."""
    assert schedule.advance_cursor(0, now=1_000_000, period_seconds=3600) == 1_000_000


def test_advance_cursor_on_time_matches_sweep_clock():
    """Firing exactly on the period boundary: the elapsed-boundary formula and
    the old sweep-clock behavior agree."""
    assert schedule.advance_cursor(1_000_000, now=1_003_600, period_seconds=3600) == 1_003_600


def test_advance_cursor_late_fire_does_not_drag_cadence_forward():
    """GA-9 AC: a task with every="168h" whose first fire is at T is next due at
    T+168h even when the fire that advanced it happened hours late."""
    period = 168 * 3600
    t = 1_000_000
    late_fire = t + period + 5 * 3600  # 5h late
    assert schedule.advance_cursor(t, now=late_fire, period_seconds=period) == t + period


def test_advance_cursor_after_long_outage_lands_on_latest_elapsed_boundary():
    """After many missed periods, the cursor snaps to the most recent elapsed
    boundary (not `now`), so it never overshoots past what actually elapsed."""
    period = 3600
    t = 1_000_000
    now = t + 10 * period + 100  # 10 periods elapsed, 100s into the 11th
    assert schedule.advance_cursor(t, now, period) == t + 10 * period


# --- GA-19: cron / wall-clock cadence + tz/DST semantics ----------------------

# --- parse_cron / resolve_tz ---------------------------------------------------

def test_parse_cron_daily_matches_expected_fields():
    spec = schedule.parse_cron("0 2 * * *")
    assert spec.minute == frozenset({0})
    assert spec.hour == frozenset({2})
    assert spec.dom_star and spec.dow_star


def test_parse_cron_dow_normalizes_7_to_0():
    """POSIX cron: both 0 and 7 mean Sunday."""
    assert schedule.parse_cron("0 0 * * 0").dow == schedule.parse_cron("0 0 * * 7").dow


@pytest.mark.parametrize("bad", [
    "", "0 2 * *", "0 2 * * * *", "60 2 * * *", "0 24 * * *", "0 2 32 * *",
    "0 2 * 13 *", "0 2 * * 8", "x 2 * * *", "5-1 * * * *",
])
def test_parse_cron_rejects_invalid(bad):
    with pytest.raises(ValueError):
        schedule.parse_cron(bad)


def test_resolve_tz_default_is_utc():
    assert schedule.resolve_tz(None) == ZoneInfo("UTC")
    assert schedule.resolve_tz("") == ZoneInfo("UTC")


def test_resolve_tz_rejects_unknown_zone():
    with pytest.raises(ValueError, match="unknown timezone"):
        schedule.resolve_tz("Not/A_Real_Zone")


# --- is_due / next_due dispatch on task shape -----------------------------------

def test_is_due_dispatches_to_cron_for_a_cron_task():
    task = {"cron": "0 2 * * *", "tz": "UTC"}
    last_fired = int(datetime(2025, 12, 31, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    before = int(datetime(2026, 1, 1, 1, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    at_slot = int(datetime(2026, 1, 1, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    assert not schedule.is_due(task, last_fired, before)
    assert schedule.is_due(task, last_fired, at_slot)


def test_next_due_requires_now_for_a_cron_task():
    task = {"cron": "0 2 * * *", "tz": "UTC"}
    with pytest.raises(ValueError):
        schedule.next_due(task, 0)  # no `now` -- must not silently misbehave


def test_next_due_for_cron_task_returns_upcoming_slot():
    task = {"cron": "0 2 * * *", "tz": "UTC"}
    now = int(datetime(2026, 1, 1, 1, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    expected = int(datetime(2026, 1, 1, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    assert schedule.next_due(task, 0, now) == expected


# --- ambient TZ independence (AC2: pure, no clock/env read) --------------------

def test_cron_due_slot_ignores_ambient_TZ_env_var(monkeypatch):
    """The module's due-check result must be a function of its arguments alone
    -- never `time.localtime`, which reads ambient TZ state."""
    task = {"cron": "0 2 * * *", "tz": "America/New_York"}
    now = datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
    baseline = schedule.cron_due_slot(task, 0, now)

    monkeypatch.setenv("TZ", "Pacific/Kiritimati")  # UTC+14, about as far as it gets
    assert schedule.cron_due_slot(task, 0, now) == baseline


# --- DST spring-forward: fires at the NEXT VALID INSTANT -----------------------

def test_cron_spring_forward_fires_at_next_valid_instant():
    """America/New_York, 2026-03-08: 02:00 local does not exist (clocks jump
    02:00 -> 03:00). A `0 2 * * *` task fires once, at the 03:00 local instant."""
    tz = ZoneInfo("America/New_York")
    task = {"cron": "0 2 * * *", "tz": "America/New_York"}
    never_fired = 0
    before_gap = datetime(2026, 3, 8, 1, 0, tzinfo=tz).timestamp()
    after_gap = datetime(2026, 3, 8, 4, 0, tzinfo=tz).timestamp()
    # The instant the wall clock actually reaches (03:00 local) -- hand-computed
    # by round-tripping the nonexistent 02:00 through .timestamp(), exactly as
    # the module docstring describes.
    expected_instant = datetime(2026, 3, 8, 2, 0, tzinfo=tz).timestamp()
    assert datetime.fromtimestamp(expected_instant, tz) == datetime(2026, 3, 8, 3, 0, tzinfo=tz)

    assert not schedule.is_due(task, before_gap, before_gap + 1800)  # 01:30, too early
    assert schedule.is_due(task, before_gap, after_gap)
    slot = schedule.cron_due_slot(task, before_gap, after_gap)
    assert slot == expected_instant

    # Fires exactly once for the whole window, including from a never-fired cursor.
    assert schedule.cron_due_slot(task, never_fired, after_gap) == expected_instant
    # No re-fire later the same day.
    assert schedule.cron_due_slot(task, slot, after_gap + 3600) is None


# --- DST fall-back: fires exactly ONCE across the repeated hour ----------------

def test_cron_fall_back_fires_exactly_once():
    """America/New_York, 2026-11-01: local 01:00 occurs twice (UTC-04:00, then
    UTC-05:00 an hour later). A `0 1 * * *` task fires once, not twice, proven
    by sweeps straddling both occurrences."""
    tz = ZoneInfo("America/New_York")
    task = {"cron": "0 1 * * *", "tz": "America/New_York"}
    before = datetime(2026, 10, 31, 12, 0, tzinfo=tz).timestamp()
    first_occurrence = datetime(2026, 11, 1, 1, 0, tzinfo=tz, fold=0).timestamp()
    second_occurrence = datetime(2026, 11, 1, 1, 0, tzinfo=tz, fold=1).timestamp()
    assert second_occurrence - first_occurrence == 3600

    # Sweep 1: lands right after the FIRST occurrence -- fires once.
    slot1 = schedule.cron_due_slot(task, before, first_occurrence + 30)
    assert slot1 == first_occurrence

    # Sweep 2: lands right after the SECOND occurrence, cursor now at slot1 --
    # must NOT fire again for the repeated hour.
    slot2 = schedule.cron_due_slot(task, slot1, second_occurrence + 30)
    assert slot2 is None

    # A single sweep straddling BOTH occurrences at once (never fired before)
    # still mints exactly one slot, not two.
    slot3 = schedule.cron_due_slot(task, before, second_occurrence + 30)
    assert slot3 == first_occurrence


# --- dedup_bucket ----------------------------------------------------------------

def test_dedup_bucket_interval_matches_period_bucket():
    task = {"every": "1h"}
    assert schedule.dedup_bucket(task, 3700) == str(int(3700 // 3600))


def test_dedup_bucket_cron_stable_within_the_same_slot():
    task = {"cron": "0 2 * * *", "tz": "UTC"}
    at_slot = int(datetime(2026, 1, 1, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    assert schedule.dedup_bucket(task, at_slot) == schedule.dedup_bucket(task, at_slot + 5)


def test_dedup_bucket_cron_differs_across_slots():
    task = {"cron": "0 2 * * *", "tz": "UTC"}
    day1 = int(datetime(2026, 1, 1, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    day2 = int(datetime(2026, 1, 2, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    assert schedule.dedup_bucket(task, day1) != schedule.dedup_bucket(task, day2)


# --- config.py: [[scheduled]] load + write round-trip -------------------------

def test_load_scheduled_from_config_toml(home):
    (home / "config.toml").write_text(
        "[maestro]\nmax_concurrency = 5\n\n"
        "[[scheduled]]\n"
        'name = "digest"\n'
        'prompt = "Summarize things"\n'
        'every = "24h"\n'
        "enabled = true\n"
    )
    cfg = config_mod.load(str(home))
    assert cfg.scheduled == [{
        "name": "digest", "prompt": "Summarize things", "every": "24h", "enabled": True,
    }]


def test_load_scheduled_absent_defaults_empty(home):
    cfg = config_mod.load(str(home))
    assert cfg.scheduled == []


def test_write_scheduled_creates_block_and_round_trips(home):
    path = home / "config.toml"
    store.atomic_write(path, "[maestro]\nmax_concurrency = 5\n")
    tasks = [{
        "name": "digest", "prompt": "Summarize PRs\nacross repos", "every": "24h",
        "kind": "implementation", "priority": 3,
        "prefix": "S", "enabled": True,
        # GA-9: repo/title plus the mint-allowlist fields must round-trip too.
        "repo": "alpha", "title": "Morning digest", "model": "sonnet",
        "effort": "high", "notes": "Skip weekends.", "depends_on": ["T-1", "T-2"],
    }]
    config_mod.write_scheduled(home, tasks)
    cfg = config_mod.load(str(home))
    assert cfg.scheduled == tasks
    # untouched sections survive
    assert "max_concurrency = 5" in path.read_text()


def test_write_scheduled_omits_unset_optional_fields(home):
    """repo/title/model/effort/notes/depends_on are all optional -- unset ones
    must not appear as e.g. `repo = "None"` in the written [[scheduled]] block."""
    tasks = [{"name": "digest", "prompt": "Summarize things", "every": "1h"}]
    config_mod.write_scheduled(home, tasks)
    block = config_mod._SCHEDULED_BLOCK_RE.search((home / "config.toml").read_text()).group()
    for field in ("repo", "title", "model", "effort", "notes", "depends_on"):
        assert f"{field} =" not in block
    cfg = config_mod.load(str(home))
    assert cfg.scheduled == tasks


def test_serialize_task_is_a_strict_allowlist():
    """GA-9 correction: the serializer must NOT become permissive -- an unknown
    key on the task dict is silently dropped, never emitted verbatim (which
    would risk re-serializing a real TOML array/table as a corrupting string)."""
    line = config_mod._serialize_task({
        "name": "digest", "prompt": "Summarize things", "every": "1h",
        "unknown_field": ["a", "b"],
    })
    assert "unknown_field" not in line


def test_toggle_one_task_does_not_strip_fields_from_another(home):
    """GA-9 blast-radius regression: `write_scheduled` regenerates the WHOLE
    array-of-tables, so toggling task A must not drop task B's title/repo."""
    tasks = [
        {"name": "a", "prompt": "Task A", "every": "1h",
         "title": "Task A title", "repo": "alpha", "enabled": True},
        {"name": "b", "prompt": "Task B", "every": "2h",
         "title": "Task B title", "repo": "beta", "enabled": True},
    ]
    config_mod.write_scheduled(home, tasks)
    # Simulate the TUI's toggle: flip only task A's `enabled`, rewrite all tasks.
    cfg = config_mod.load(str(home))
    for t in cfg.scheduled:
        if t["name"] == "a":
            t["enabled"] = False
    config_mod.write_scheduled(home, cfg.scheduled)
    reloaded = config_mod.load(str(home))
    task_b = next(t for t in reloaded.scheduled if t["name"] == "b")
    assert task_b["title"] == "Task B title"
    assert task_b["repo"] == "beta"


def test_write_scheduled_replaces_existing_blocks_only(home):
    path = home / "config.toml"
    store.atomic_write(path, (
        "[maestro]\nmax_concurrency = 5\n\n"
        "[[scheduled]]\nname = \"old\"\nprompt = \"old prompt\"\nevery = \"1h\"\n"
    ))
    new_tasks = [{"name": "new", "prompt": "new prompt", "every": "2h"}]
    config_mod.write_scheduled(home, new_tasks)
    cfg = config_mod.load(str(home))
    assert cfg.scheduled == new_tasks
    assert "old" not in path.read_text()
    assert "max_concurrency = 5" in path.read_text()


def test_write_scheduled_cron_and_tz_round_trip(home):
    """GA-19 AC: `cron`/`tz` are in the `_SCHEDULED_FIELDS` allowlist, so they
    round-trip through config.toml exactly like `every` always has -- and a
    cron task carries no `every` key at all."""
    tasks = [{
        "name": "digest", "prompt": "Summarize things", "cron": "0 9 * * 1",
        "tz": "America/New_York", "kind": "implementation",
        "priority": 3, "enabled": True,
    }]
    config_mod.write_scheduled(home, tasks)
    cfg = config_mod.load(str(home))
    assert cfg.scheduled == tasks
    assert "every" not in cfg.scheduled[0]


def test_write_scheduled_empty_list_removes_all_blocks(home):
    path = home / "config.toml"
    store.atomic_write(path, (
        "[maestro]\nmax_concurrency = 5\n\n"
        "[[scheduled]]\nname = \"old\"\nprompt = \"old prompt\"\nevery = \"1h\"\n"
    ))
    config_mod.write_scheduled(home, [])
    cfg = config_mod.load(str(home))
    assert cfg.scheduled == []
    assert "old" not in path.read_text()
