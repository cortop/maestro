"""Unit tests for maestro/schedule.py (cadence parsing + due-checks) and the
config.py TOML read/write round-trip for `[[scheduled]]` tasks."""
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
        "approval_tier": 1, "kind": "implementation", "priority": 3,
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
