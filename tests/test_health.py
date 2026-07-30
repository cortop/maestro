import io
import json
import os
import subprocess
import sys

from maestro import cli, claims, dispatcher as disp, health, ops, store
from maestro.config import Config
from maestro.statemachine import Phase

from test_dispatcher import _EphemeralSessions, _seed, _seed_with_deps


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


# --- L-12: doctor v2 check registry --------------------------------------------


def test_stale_threshold_derived_from_plist_interval(tmp_path):
    plist = tmp_path / "agent.plist"
    plist.write_text("<key>StartInterval</key>\n  <integer>100</integer>\n")
    assert health.stale_threshold(plist=plist) == 100 * health.STALE_INTERVAL_FACTOR


def test_stale_threshold_falls_back_without_a_plist():
    assert health.stale_threshold(plist="/nonexistent") == health.DEFAULT_STALE_THRESHOLD


def test_check_heartbeat_uses_the_derived_threshold(home, cfg, tmp_path):
    plist = tmp_path / "agent.plist"
    plist.write_text("<key>StartInterval</key>\n  <integer>100</integer>\n")  # threshold 600
    store.write_json(home / "derived" / ".heartbeat.json",
                     {"ts": "x", "epoch": store.now_epoch() - 650, "spawned": 0})
    result = health.check_heartbeat(cfg, store.now_epoch(), plist=plist)
    assert result["threshold_s"] == 600
    assert result["stale"] is True
    assert result["status"] == "fail"


def test_check_backup_age_ok_when_disabled(home, cfg):
    cfg.backup_interval = 0
    result = health.check_backup_age(cfg, store.now_epoch())
    assert result == {"name": "backup_age", "status": "ok", "detail": "backups disabled", "age_s": None}


def test_check_backup_age_warns_when_stale(home, cfg):
    cfg.backup_interval = 100
    now = store.now_epoch()
    store.write_json(home / "derived" / ".backup_cursor.json", {"epoch": now - 1000})
    result = health.check_backup_age(cfg, now)
    assert result["status"] == "warn"
    assert result["age_s"] == 1000


def test_check_backup_age_ok_when_fresh(home, cfg):
    cfg.backup_interval = 3600
    now = store.now_epoch()
    store.write_json(home / "derived" / ".backup_cursor.json", {"epoch": now - 10})
    result = health.check_backup_age(cfg, now)
    assert result["status"] == "ok"


def test_check_claim_age_ok_with_no_claims(home, cfg):
    result = health.check_claim_age(cfg, store.now_epoch())
    assert result == {"name": "claim_age", "status": "ok", "detail": "no live claims",
                       "oldest_key": None, "oldest_age_s": None}


def test_check_claim_age_warns_past_max_session_seconds(home, cfg):
    cfg.max_session_seconds = 100
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")
    data = claims.read_claim(home, "T-1")
    data["epoch"] = store.now_epoch() - 10_000
    store.write_json(claims.claim_path(home, "T-1"), data)

    result = health.check_claim_age(cfg, store.now_epoch())
    assert result["status"] == "warn"
    assert result["oldest_key"] == "T-1"
    assert result["oldest_age_s"] >= 10_000


def test_check_launchctl_ok_when_not_loaded():
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 1, stdout="")
    result = health.check_launchctl(run=fake_run)
    assert result == {"name": "launchctl", "status": "ok", "detail": "not loaded", "last_exit_code": None}


def test_check_launchctl_fails_on_nonzero_last_exit():
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, stdout='"LastExitStatus" = 1;\n')
    result = health.check_launchctl(run=fake_run)
    assert result["status"] == "fail"
    assert result["last_exit_code"] == 1


def test_check_launchctl_ok_on_zero_last_exit():
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, stdout='"LastExitStatus" = 0;\n')
    result = health.check_launchctl(run=fake_run)
    assert result["status"] == "ok"
    assert result["last_exit_code"] == 0


def test_check_dead_letters_reports_ages(home, cfg):
    dl = home / "tickets" / "_deadletter"
    dl.mkdir(parents=True)
    (dl / "T-1.md").write_text("dead")
    result = health.check_dead_letters(cfg, store.now_epoch())
    assert result["status"] == "warn"
    assert "T-1" in result["ages_s"]


def test_check_dead_letters_ok_when_none(home, cfg):
    result = health.check_dead_letters(cfg, store.now_epoch())
    assert result == {"name": "dead_letters", "status": "ok", "detail": "none", "ages_s": {}}


def test_check_depends_on_reports_missing_key(home, cfg):
    _seed_with_deps(home, "T-1", Phase.READY, depends_on=["TYPO-9"])
    result = health.check_depends_on(cfg, store.now_epoch())
    assert result["status"] == "warn"
    assert {"key": "T-1", "dep": "TYPO-9"} in result["missing"]
    assert result["cycles"] == []


def test_check_depends_on_ignores_an_archived_done_dependency(home, cfg):
    """A finished, archived dependency is not a 'missing' dep -- that would
    falsely flag every completed-and-archived ticket's dependents forever."""
    _seed(home, "T-dep", Phase.DONE)
    ops.archive_done(cfg)
    _seed_with_deps(home, "T-1", Phase.READY, depends_on=["T-dep"])

    result = health.check_depends_on(cfg, store.now_epoch())
    assert result["missing"] == []
    assert result["status"] == "ok"


def test_check_depends_on_detects_a_cycle(home, cfg):
    _seed_with_deps(home, "T-1", Phase.READY, depends_on=["T-2"])
    _seed_with_deps(home, "T-2", Phase.READY, depends_on=["T-1"])
    result = health.check_depends_on(cfg, store.now_epoch())
    assert result["status"] == "fail"
    assert len(result["cycles"]) >= 1
    assert set(result["cycles"][0]) == {"T-1", "T-2"}


def test_check_depends_on_ok_with_no_deps(home, cfg):
    _seed(home, "T-1", Phase.READY)
    result = health.check_depends_on(cfg, store.now_epoch())
    assert result == {"name": "depends_on", "status": "ok",
                       "detail": "0 missing dep(s), 0 cycle(s)", "missing": [], "cycles": []}


def test_doctor_cli_includes_check_registry(home, cfg):
    """AC3: `maestro doctor` runs the full check registry via the real CLI."""
    _seed(home, "T-1", Phase.READY)
    code, out = _sweep(home)
    assert code == 0
    names = {c["name"] for c in out["checks"]}
    assert names == {"heartbeat", "backup_age", "claim_age", "dead_letters",
                      "depends_on", "launchctl"}
    assert all(c["status"] in {"ok", "warn", "fail"} for c in out["checks"])
