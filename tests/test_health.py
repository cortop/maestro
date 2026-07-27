import io
import json
import sys

from maestro import cli, dispatcher as disp, health, store
from maestro.config import Config
from maestro.statemachine import Phase

from test_dispatcher import _EphemeralSessions, _seed


def _sweep(home):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(home), "doctor"])
    finally:
        sys.stdout = old
    return code, json.loads(buf.getvalue())


# --- spawn_budget_per_hour knob -----------------------------------------------


def test_spawn_budget_uses_configured_knob(home):
    (home / "config.toml").write_text("[maestro]\nrunaway_spawns_per_hour = 5\n")
    code, out = _sweep(home)
    assert code == 0
    assert out["spawn_budget_per_hour"] == 5


def test_spawn_budget_derived_from_spawn_floor_when_knob_absent(home, cfg):
    for i in range(3):
        _seed(home, f"T-{i}", Phase.READY)
    import math
    expected = len(disp.list_keys(home)) * math.ceil(3600 / disp.spawn_floor(cfg))
    assert health.spawn_budget(cfg) == expected
    # Same number, proven through the real CLI (no config.toml -> knob absent).
    _code, out = _sweep(home)
    assert out["spawn_budget_per_hour"] == expected


def test_spawn_budget_falls_back_when_floor_disabled(home):
    for i in range(2):
        _seed(home, f"T-{i}", Phase.READY)
    cfg = Config(home=home, min_spawn_interval=0, reconcile_steady_interval=300)
    import math
    expected = len(disp.list_keys(home)) * math.ceil(3600 / max(300, 60))
    assert health.spawn_budget(cfg) == expected
    assert disp.spawn_floor(cfg) == 0  # confirms the fallback path is exercised


def test_runaway_check_disabled_when_knob_is_zero(home, cfg):
    (home / "config.toml").write_text("[maestro]\nrunaway_spawns_per_hour = 0\n")
    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg2 = Config(home=home, min_spawn_interval=0, max_concurrency=1)
    sessions = _EphemeralSessions()
    for i in range(50):
        disp.dispatch(cfg2, sessions, now=1000 + i)
    code, out = _sweep(home)
    assert out["spawn_budget_per_hour"] == 0
    assert out["runaway"] is False
    assert code == 0


# --- doctor contract preserved + extended --------------------------------------


def test_doctor_keeps_original_fields_and_stale_semantics(home):
    store.write_json(home / "derived" / ".heartbeat.json",
                     {"ts": "x", "epoch": store.now_epoch() - 2000,
                      "spawned": 0, "active": 0, "throttled": 0, "due": 0})
    code, out = _sweep(home)
    assert out["heartbeat"]["epoch"] is not None
    assert out["heartbeat_age_s"] > 1800
    assert out["dead_letters"] == []
    assert out["stale"] is True
    assert code == 0  # stale alone must not trip runaway


def test_doctor_reports_new_fields(home):
    code, out = _sweep(home)
    assert "total" in out["spawns_last_hour"] and "by_key" in out["spawns_last_hour"]
    assert isinstance(out["throttled_last_sweep"], int)
    assert isinstance(out["spawn_budget_per_hour"], int)
    assert isinstance(out["runaway"], bool)
    assert code == 0


def test_throttled_last_sweep_reflects_prior_real_sweep(home, cfg):
    for i in range(3):
        _seed(home, f"T-{i}", Phase.READY)
    cfg.min_spawn_interval = 300
    sessions = _EphemeralSessions()
    disp.dispatch(cfg, sessions, now=1000)          # spawns all three
    report = disp.dispatch(cfg, sessions, now=1001)  # all three throttled
    assert len(report.throttled) == 3
    _code, out = _sweep(home)
    assert out["throttled_last_sweep"] == 3


# --- replay the incident through the real surface ------------------------------
#
# `health.report` reads spawn history relative to the REAL wall clock
# (`store.now_epoch()`, same clock `cmd_doctor` uses), so these sweeps must be
# timestamped near real "now" -- an arbitrary synthetic epoch (as the other
# dispatcher tests use) would fall outside the trailing-hour window by the time
# `cli.main(["doctor"])` reads it back.


def test_incident_replay_trips_runaway_with_floor_disabled(home):
    """Four tickets that never advance, spawn floor OFF: real sweeps at
    the 2026-07-19 cadence (~11s) exceed the derived budget and doctor trips."""
    for k in ("T-1", "T-2", "T-3", "T-4"):
        _seed(home, k, Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=4, min_spawn_interval=0)
    sessions = _EphemeralSessions()
    t0, step = store.now_epoch(), 11
    for i in range(100):  # ~18 minutes of 11s sweeps, well inside the 1h window
        disp.dispatch(cfg, sessions, now=t0 + i * step)

    code, out = _sweep(home)
    assert out["runaway"] is True
    assert code == 1


def test_same_tickets_under_default_floor_do_not_trip(home):
    """The same shape, but with the default spawn floor engaged, stays healthy."""
    for k in ("T-1", "T-2", "T-3", "T-4"):
        _seed(home, k, Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=4)  # min_spawn_interval defaults to
                                                 # reconcile_steady_interval (300)
    sessions = _EphemeralSessions()
    t0, step = store.now_epoch(), 11
    for i in range(100):
        disp.dispatch(cfg, sessions, now=t0 + i * step)

    code, out = _sweep(home)
    assert out["runaway"] is False
    assert code == 0
