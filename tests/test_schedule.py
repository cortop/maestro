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
    }]
    config_mod.write_scheduled(home, tasks)
    cfg = config_mod.load(str(home))
    assert cfg.scheduled == tasks
    # untouched sections survive
    assert "max_concurrency = 5" in path.read_text()


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
