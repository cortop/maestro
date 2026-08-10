import io
import json
import os
import subprocess
import sys

from maestro import cli, claims, dispatcher as disp, health, ops, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from test_dispatcher import _EphemeralSessions, _seed, _seed_with_deps
from test_repo_preflight import _repo_table, _seed_bound_ticket


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
    # GA-14: spawn_budget is phase-aware now, but these tickets are READY --
    # weight 1, same as before weighting -- so the formula (and number) here
    # is unchanged; only an `implementing` ticket gets the bigger allowance
    # (see test_same_tickets_under_default_floor_do_not_trip).
    for i in range(3):
        _seed(home, f"T-{i}", Phase.READY)
    import math
    expected = len(disp.list_keys(home)) * math.ceil(3600 / disp.spawn_floor(cfg))
    assert health.spawn_budget(cfg) == expected
    # Same number, proven through the real CLI (no config.toml -> knob absent).
    _code, out = _sweep(home)
    assert out["spawn_budget_per_hour"] == expected


def test_spawn_budget_falls_back_when_floor_disabled(home):
    # GA-14: READY tickets weight 1, so this stays a plain session count too.
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
    # GA-14: spawn_rate/spawn_budget are agent-equivalents now, not a bare
    # session count -- the payload names the unit so a human reading a bare
    # number knows it isn't sessions.
    assert out["spawn_rate_unit"] == "agent-equivalents"
    # GA-11: added beside the spawn-rate fields above, not folded into them.
    assert out["spend_today_usd"] == 0.0
    assert out["spend_ceiling_usd"] is None
    assert out["spend_unavailable"] is False
    assert isinstance(out["spend_unattributed_sessions"], int)
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
    the 2026-07-19 cadence (~11s) exceed the derived budget and doctor trips
    -- and, GA-14, trips in strictly fewer sweeps than session-counting did.

    Pre-weighting, this exact replay (four keys' worth of un-throttled ~11s
    spawns against a 48-session/hour budget, i.e. `n_keys * ceil(3600 /
    effective_floor)`) empirically crosses around sweep 15-16 (the watchdog's
    own no-progress reaping staggers the raw spawn count, so it's not an exact
    `budget / spawns_per_sweep` division). Weighted, each `implementing` spawn
    now counts as `disp.spawn_weight(cfg, "implementing")` (21 by default)
    agent-equivalents against a budget that only assumes HALF that per spawn
    (`health._budget_weight` -- see its docstring: budgeting the full
    worst-case weight AS the healthy baseline would make the detector no more
    sensitive than before, since it multiplies both sides of `rate > budget`
    by the same factor). Empirically this crosses by sweep 7-9; asserted at
    sweep 12 for headroom against the auto-brake's own jitter (ops.fail's
    exponential backoff is randomized) without flirting with flakiness --
    still well under the pre-weighting ~15-16.
    """
    for k in ("T-1", "T-2", "T-3", "T-4"):
        _seed(home, k, Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=4, min_spawn_interval=0)
    sessions = _EphemeralSessions()
    t0, step = store.now_epoch(), 11
    for i in range(12):
        disp.dispatch(cfg, sessions, now=t0 + i * step)
    code, out = _sweep(home)
    assert out["runaway"] is True and code == 1  # strictly fewer than the
                                                  # ~15-16 sweeps session-counting needed

    for i in range(12, 100):  # ~18 minutes of 11s sweeps, well inside the 1h window
        disp.dispatch(cfg, sessions, now=t0 + i * step)
    code, out = _sweep(home)
    assert out["runaway"] is True
    assert code == 1


def test_same_tickets_under_default_floor_do_not_trip(home):
    """The same shape, but with the default spawn floor engaged, stays
    healthy -- the GA-14 central-design-trap check: with the floor respected,
    each of the 4 IMPLEMENTING tickets spawns only ~4 times in this window,
    weighted rate 16 * 21 == 336, comfortably under the phase-aware budget
    (health.spawn_budget), which is NOT silently redefined down to a bare
    session count just because every ticket happens to be `implementing`."""
    for k in ("T-1", "T-2", "T-3", "T-4"):
        _seed(home, k, Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=4)  # min_spawn_interval defaults to
                                                 # reconcile_steady_interval (300)
    sessions = _EphemeralSessions()
    t0, step = store.now_epoch(), 11
    for i in range(100):
        disp.dispatch(cfg, sessions, now=t0 + i * step)

    W_implementing = disp.spawn_weight(cfg, Phase.IMPLEMENTING.value)  # 1 + 20*1 == 21
    assert health.spawn_rate(home, t0 + 99 * step)["total"] == 16 * W_implementing
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
                       "oldest_key": None, "oldest_age_s": None, "stale_output_s": None}


def test_check_claim_age_warns_past_leading_fraction_of_max_session_seconds(home, cfg):
    # QW-4: warn at 0.5 x max_session_seconds (the leading indicator), not at the
    # full threshold -- by the time a claim IS the full threshold old, the watchdog
    # (which reaps at that same value) has already killed it.
    cfg.max_session_seconds = 100
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")
    data = claims.read_claim(home, "T-1")
    data["epoch"] = store.now_epoch() - 60  # 0.6 x threshold
    store.write_json(claims.claim_path(home, "T-1"), data)

    result = health.check_claim_age(cfg, store.now_epoch())
    assert result["status"] == "warn"
    assert result["oldest_key"] == "T-1"
    assert result["oldest_age_s"] >= 60


def test_check_claim_age_ok_below_leading_fraction_of_max_session_seconds(home, cfg):
    cfg.max_session_seconds = 100
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")
    data = claims.read_claim(home, "T-1")
    data["epoch"] = store.now_epoch() - 40  # 0.4 x threshold
    store.write_json(claims.claim_path(home, "T-1"), data)

    result = health.check_claim_age(cfg, store.now_epoch())
    assert result["status"] == "ok"


def test_check_claim_age_full_key_set_and_unchanged_names_types(home, cfg):
    cfg.max_session_seconds = 100
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")
    result = health.check_claim_age(cfg, store.now_epoch())
    assert set(result.keys()) == {
        "name", "status", "detail", "oldest_key", "oldest_age_s", "stale_output_s",
    }
    assert isinstance(result["name"], str)
    assert isinstance(result["status"], str)
    assert isinstance(result["detail"], str)
    assert isinstance(result["oldest_key"], str)
    assert isinstance(result["oldest_age_s"], int)
    assert result["stale_output_s"] is None  # no log_path recorded


def test_check_claim_age_stale_output_s_from_log_path_mtime(home, cfg, tmp_path):
    log_file = tmp_path / "session.stream.jsonl"
    log_file.write_text("{}")
    now = store.now_epoch()
    old_mtime = now - 500
    os.utime(log_file, (old_mtime, old_mtime))

    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1", log_path=str(log_file))
    result = health.check_claim_age(cfg, now)
    assert result["stale_output_s"] >= 500


def test_doctor_cli_warns_and_names_key_for_mid_threshold_claim(home):
    """AC4: real `maestro doctor --json` over a temp home with a claim aged
    0.6 x max_session_seconds reports warn and names the key -- QA per CLAUDE.md,
    the real CLI, nothing mocked."""
    (home / "config.toml").write_text("[maestro]\nmax_session_seconds = 100\n")
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")
    data = claims.read_claim(home, "T-1")
    data["epoch"] = store.now_epoch() - 60  # 0.6 x threshold
    store.write_json(claims.claim_path(home, "T-1"), data)

    code, out = _sweep(home)
    assert code == 0
    check = next(c for c in out["checks"] if c["name"] == "claim_age")
    assert check["status"] == "warn"
    assert check["oldest_key"] == "T-1"


def test_check_claim_no_output_ok_with_no_claims(home, cfg):
    result = health.check_claim_no_output(cfg, store.now_epoch())
    assert result == {"name": "claim_no_output", "status": "ok", "detail": "no stale-output claims",
                       "stale_key": None, "stale_age_s": None}


def test_check_claim_no_output_disabled_when_timeout_zero(home, cfg, tmp_path):
    cfg.no_output_timeout = 0
    log_file = tmp_path / "T-1.jsonl"
    log_file.write_text("{}\n")
    old = store.now_epoch() - 1_000_000
    os.utime(log_file, (old, old))
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1", log_path=str(log_file))

    result = health.check_claim_no_output(cfg, store.now_epoch())
    assert result["status"] == "ok"
    assert result["detail"] == "no-output watchdog disabled"


def test_check_claim_no_output_warns_on_stale_log(home, cfg, tmp_path):
    """AC5: distinguishable by name from `claim_age` -- this fires purely off
    log mtime, independent of claim epoch."""
    cfg.no_output_timeout = 300
    log_file = tmp_path / "T-1.jsonl"
    log_file.write_text("{}\n")
    old = store.now_epoch() - 1000
    os.utime(log_file, (old, old))
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1", log_path=str(log_file))

    result = health.check_claim_no_output(cfg, store.now_epoch())
    assert result["name"] == "claim_no_output"
    assert result["status"] == "warn"
    assert result["stale_key"] == "T-1"
    assert result["stale_age_s"] >= 1000


def test_check_claim_no_output_exempts_claim_with_no_log_path(home, cfg):
    cfg.no_output_timeout = 300
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")  # no log_path

    result = health.check_claim_no_output(cfg, store.now_epoch())
    assert result["status"] == "ok"


def test_check_launchctl_ok_when_not_loaded(cfg):
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 1, stdout="")
    result = health.check_launchctl(cfg, run=fake_run)
    assert result == {"name": "launchctl", "status": "ok", "detail": "not loaded", "last_exit_code": None}


def test_check_launchctl_fails_on_nonzero_last_exit(cfg):
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, stdout='"LastExitStatus" = 1;\n')
    result = health.check_launchctl(cfg, run=fake_run)
    assert result["status"] == "fail"
    assert result["last_exit_code"] == 1


def test_check_launchctl_ok_on_zero_last_exit(cfg):
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, stdout='"LastExitStatus" = 0;\n')
    result = health.check_launchctl(cfg, run=fake_run)
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


# --- spawn_floor check (GA-8) ---------------------------------------------------


def test_check_spawn_floor_warns_when_disabled(home, cfg):
    cfg.min_spawn_interval = 0
    result = health.check_spawn_floor(cfg, store.now_epoch())
    assert result["name"] == "spawn_floor"
    assert result["status"] == "warn"
    assert result["floor_s"] == 0
    assert "max_concurrency" in result["detail"]


def test_check_spawn_floor_ok_when_set(home, cfg):
    cfg.min_spawn_interval = 300
    result = health.check_spawn_floor(cfg, store.now_epoch())
    assert result["status"] == "ok"
    assert result["floor_s"] == 300


def test_report_exposes_spawn_floor_seconds(home, cfg):
    cfg.min_spawn_interval = 45
    rpt = health.report(cfg, store.now_epoch())
    assert rpt["spawn_floor_s"] == 45


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


def test_doctor_resolves_per_home_plist_ignoring_a_legacy_decoy(home, tmp_path, monkeypatch):
    """MR-1 AC5: doctor must resolve the per-home (slugged) plist's
    StartInterval for the heartbeat threshold, ignoring a same-machine decoy
    legacy plist, and query launchctl with the slugged label."""
    from maestro import fleet

    fake_home = tmp_path / "fakehome"
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    lbl = fleet.label(home)
    agents_dir = fake_home / "Library" / "LaunchAgents"
    (agents_dir / f"{lbl}.plist").write_text(
        "<key>StartInterval</key>\n<integer>111</integer>\n")
    (agents_dir / f"{fleet.LEGACY_LABEL}.plist").write_text(
        "<key>StartInterval</key>\n<integer>999</integer>\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    argv_log = tmp_path / "argv.log"
    stub = fake_bin / "launchctl"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{argv_log}"\nexit 0\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    _code, out = _sweep(home)
    hb_check = next(c for c in out["checks"] if c["name"] == "heartbeat")
    assert hb_check["threshold_s"] == 111 * health.STALE_INTERVAL_FACTOR

    lines = [l for l in argv_log.read_text().splitlines() if l.strip()]
    assert any(line == f"list {lbl}" for line in lines)
    assert not any(line == f"list {fleet.LEGACY_LABEL}" for line in lines)


def test_doctor_cli_includes_check_registry(home, cfg):
    """AC3: `maestro doctor` runs the full check registry via the real CLI."""
    _seed(home, "T-1", Phase.READY)
    code, out = _sweep(home)
    assert code == 0
    names = {c["name"] for c in out["checks"]}
    assert names == {"heartbeat", "backup_age", "claim_age", "claim_no_output", "dead_letters",
                      "depends_on", "launchctl", "repo_preflight",
                      "unknown_repo_bindings", "missing_reconcile_skill",
                      "reconciler_permissions", "spawn_floor", "daily_spend",
                      "gh_credential_reachability", "ollama_models"}
    assert all(c["status"] in {"ok", "warn", "fail"} for c in out["checks"])


# --- T-46: run_checks iterates CHECKS instead of slicing it --------------------


def test_run_checks_surfaces_a_stub_check_at_any_registry_position(cfg, monkeypatch):
    """AC1: a stub check inserted into health.CHECKS surfaces in run_checks'
    results whether it's prepended, spliced into the middle, or appended --
    proving run_checks iterates the whole registry rather than slicing off a
    fixed prefix/suffix."""
    def stub(name):
        def _check(cfg, now, **kw):
            return {"name": name, "status": "ok", "detail": "stub"}
        return _check

    original = health.CHECKS
    mid = len(original) // 2
    positions = {
        "position 0": (stub("stub_check"),) + original,
        "the middle": original[:mid] + (stub("stub_check"),) + original[mid:],
        "the end": original + (stub("stub_check"),),
    }
    for where, checks in positions.items():
        monkeypatch.setattr(health, "CHECKS", checks)
        names = {r["name"] for r in health.run_checks(cfg, 1000)}
        assert "stub_check" in names, f"stub inserted at {where} did not surface"


def test_run_checks_calls_heartbeat_once_and_launchctl_is_registered(cfg):
    """AC2: check_heartbeat must not run twice -- once explicitly, once again
    via the registry -- and check_launchctl must be a genuine CHECKS entry,
    not appended out-of-band after the loop."""
    checks = health.run_checks(cfg, 1000)
    names = [r["name"] for r in checks]
    assert names.count("heartbeat") == 1
    assert "launchctl" in names
    assert health.check_launchctl in health.CHECKS


def test_doctor_json_check_names_and_exit_code_match_pre_change_baseline(home):
    """AC3: real `maestro doctor` (JSON stdout) over a temp MAESTRO_HOME prints
    the same check-name set and the same exit code (0) as the captured
    pre-change baseline -- iterating CHECKS instead of slicing it must not
    add, drop, or rename a single check. (T-48 legitimately grew the registry
    by one -- `claim_no_output` -- after this baseline was captured; folded in
    here rather than re-captured, since T-46's own invariant, "iterating
    doesn't silently add/drop/rename", still holds for every other name. T-33
    grew it by one more -- `ollama_models` -- same treatment.)"""
    baseline_names = {
        "heartbeat", "backup_age", "claim_age", "claim_no_output", "dead_letters",
        "depends_on", "repo_preflight", "unknown_repo_bindings",
        "missing_reconcile_skill", "reconciler_permissions", "spawn_floor",
        "daily_spend", "gh_credential_reachability", "launchctl", "ollama_models",
    }
    code, out = _sweep(home)
    assert code == 0
    assert {c["name"] for c in out["checks"]} == baseline_names


# --- GA-8: negative min_spawn_interval rejected at load, 0 stays legal ---------


def test_doctor_over_real_home_warns_on_disabled_floor(tmp_path):
    """QA per CLAUDE.md: min_spawn_interval = 0 in a real config.toml, over a real
    `maestro` CLI call -- nothing mocked. The doctor payload carries both the warn
    check and the effective-floor field."""
    from maestro import cli

    home = tmp_path / "home"
    for d in ("events", "inbox", "tickets", "worktrees", "derived/snapshots", "derived/cursors"):
        (home / d).mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[maestro]\nmin_spawn_interval = 0\n")

    code, out = _sweep(home)
    assert code == 0
    assert out["spawn_floor_s"] == 0
    check = next(c for c in out["checks"] if c["name"] == "spawn_floor")
    assert check["status"] == "warn"


def test_negative_min_spawn_interval_rejected_at_load(tmp_path, capsys):
    """AC: a negative min_spawn_interval fails `maestro doctor` closed -- exit 2,
    an `error:` line naming the key, the offending value, and config.toml."""
    from maestro import cli

    home = tmp_path / "home"
    for d in ("events", "inbox", "tickets", "worktrees", "derived/snapshots", "derived/cursors"):
        (home / d).mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[maestro]\nmin_spawn_interval = -1\n")

    code = cli.main(["--home", str(home), "doctor"])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("error:")
    assert "min_spawn_interval" in err
    assert "-1" in err
    assert "config.toml" in err


def test_negative_min_spawn_interval_fails_dispatch_closed(tmp_path, capsys):
    """The same bad config fails closed on the sweep path too: `config.load`
    raises before `dispatch()` is ever reached, so no heartbeat/ledger is written."""
    from maestro import cli, config as config_mod, store as store_mod

    home = tmp_path / "home"
    for d in ("events", "inbox", "tickets", "worktrees", "derived/snapshots", "derived/cursors"):
        (home / d).mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[maestro]\nmin_spawn_interval = -1\n")

    try:
        config_mod.load(str(home))
        assert False, "config.load should have raised"
    except store_mod.MaestroError:
        pass

    code = cli.main(["--home", str(home), "dispatch"])
    assert code == 2
    assert not (home / "derived" / ".heartbeat.json").exists()
    assert not (home / "derived" / ".spawn_ledger.json").exists()


# --- GA-16: reconciler permission surface -------------------------------------

from conftest import git as _git  # noqa: E402


def _init_plain_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / "f.txt").write_text("hi\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)


def _write_repo_settings(repo, *, allow=None, deny=None, local=False):
    fname = "settings.local.json" if local else "settings.json"
    payload = {"permissions": {}}
    if allow is not None:
        payload["permissions"]["allow"] = allow
    if deny is not None:
        payload["permissions"]["deny"] = deny
    store.write_json(repo / ".claude" / fname, payload)


def _install_dummy_reconcile_skill(repo):
    """Satisfies the unrelated missing_reconcile_skill check so a --strict
    assertion below is about reconciler_permissions specifically."""
    d = repo / ".claude" / "commands"
    d.mkdir(parents=True, exist_ok=True)
    (d / "maestro-reconcile-triaging.md").write_text("# stub\n")


def test_reconciler_permissions_registered_and_never_blocks_a_spawn(home, tmp_path, monkeypatch):
    """AC1: the check is registered, iterates referenced_repo_bindings, and never
    blocks a spawn -- proven by a real sweep over a temp home whose bound repo
    carries no settings file at all."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    _seed(home, "T-1", Phase.IMPLEMENTING)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.spawned == ["T-1"]  # the missing grant never blocked the spawn

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "warn"
    assert "default" in check["missing_by_repo"]


def test_reconciler_permissions_missing_patterns_match_shared_constant(home, tmp_path, monkeypatch):
    """AC3 + AC6: a repo granted only Bash(maestro:*) is reported not-ok with
    git/gh/the test runner named as missing, verbatim against the shared
    constant both this check and the spawn-time grant (cli._reconciler_tool_grants)
    read."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    _write_repo_settings(repo, allow=["Bash(maestro:*)"])
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "warn"
    missing = check["missing_by_repo"]["default"]
    assert "Bash(maestro:*)" not in missing        # already granted
    assert set(missing) == set(disp.RECONCILER_REQUIRED_TOOLS) - {"Bash(maestro:*)"}
    for tool in missing:
        assert tool in disp.RECONCILER_REQUIRED_TOOLS   # verbatim, not paraphrased
        assert tool in check["detail"]


def test_reconciler_permissions_union_across_settings_layers(home, tmp_path, monkeypatch):
    """AC2 + AC5: a grant present in ANY layer -- repo settings.local.json, repo
    settings.json, or the injected user-scope settings.json -- satisfies the
    requirement; each layer is exercised independently here."""
    user_settings = tmp_path / "user-settings.json"
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(user_settings))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    required = list(disp.RECONCILER_REQUIRED_TOOLS)
    third = len(required) // 3 or 1
    _write_repo_settings(repo, allow=required[:third], local=True)
    _write_repo_settings(repo, allow=required[third:2 * third])
    store.write_json(user_settings, {"permissions": {"allow": required[2 * third:]}})
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "ok"
    assert check["missing_by_repo"] == {}


def test_reconciler_permissions_user_scope_alone_satisfies(home, tmp_path, monkeypatch):
    """AC5: a grant present ONLY in the injected user-scope file (the repo
    itself carries none) still satisfies the check."""
    user_settings = tmp_path / "user-settings.json"
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(user_settings))
    store.write_json(user_settings, {"permissions": {"allow": list(disp.RECONCILER_REQUIRED_TOOLS)}})
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "ok"


def test_reconciler_permissions_deny_rule_reported_in_detail(home, tmp_path, monkeypatch):
    """AC2: a deny rule that would block a required tool is reported in the
    detail, even though the same tool is also present in the allow list."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    _write_repo_settings(repo, allow=list(disp.RECONCILER_REQUIRED_TOOLS), deny=["Bash(gh:*)"])
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "warn"
    assert "Bash(gh:*)" in check["missing_by_repo"]["default"]
    assert "Bash(gh:*)" in check["detail"]
    assert "denied" in check["detail"]


def test_reconciler_permissions_ok_when_permission_mode_bypasses(home, tmp_path, monkeypatch):
    """AC4: permission_mode = bypassPermissions makes the question moot for the
    home -- ok, never a false warning, even with no settings file anywhere."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    cfg = Config(home=home, repo_path=str(repo), permission_mode="bypassPermissions")

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "ok"
    assert "bypassed" in check["detail"]


def test_doctor_strict_flag_gates_on_unsatisfied_checks(home, tmp_path, monkeypatch, capsys):
    """AC7 + AC10: the global doctor exit contract stays 0 no matter what;
    `--strict` exits 1 while the reconciler_permissions check is not ok and 0
    once every check (including it) is ok. QA over the real CLI -- only the
    user-home settings location is mocked, never the developer's real one, and
    nothing spawns a session."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    _install_dummy_reconcile_skill(repo)
    (home / "config.toml").write_text(
        f'[maestro]\nbackup_interval = 0\ndaily_spend_ceiling_usd = 50.0\n\n'
        f'[repos.alpha]\npath = "{repo}"\ndefault = true\n')
    _seed_bound_ticket(home, "T-1", "alpha")

    rc = cli.main(["--home", str(home), "doctor"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    check = next(c for c in out["checks"] if c["name"] == "reconciler_permissions")
    assert check["status"] == "warn"
    assert "alpha" in check["detail"]

    rc_strict = cli.main(["--home", str(home), "doctor", "--strict"])
    capsys.readouterr()
    assert rc_strict == 1

    _write_repo_settings(repo, allow=list(disp.RECONCILER_REQUIRED_TOOLS))

    rc2 = cli.main(["--home", str(home), "doctor"])
    out2 = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    check2 = next(c for c in out2["checks"] if c["name"] == "reconciler_permissions")
    assert check2["status"] == "ok"

    rc2_strict = cli.main(["--home", str(home), "doctor", "--strict"])
    capsys.readouterr()
    assert rc2_strict == 0

    # AC10: repeat with the grant present ONLY in the injected user-scope file --
    # remove the repo-scope grant that just satisfied it above, so this leg is
    # unambiguously exercising the user-scope layer, still through the real CLI.
    (repo / ".claude" / "settings.json").unlink()
    user_settings_path = tmp_path / "no-user-settings.json"
    store.write_json(user_settings_path, {"permissions": {"allow": list(disp.RECONCILER_REQUIRED_TOOLS)}})

    rc3 = cli.main(["--home", str(home), "doctor"])
    out3 = json.loads(capsys.readouterr().out)
    assert rc3 == 0
    check3 = next(c for c in out3["checks"] if c["name"] == "reconciler_permissions")
    assert check3["status"] == "ok"

    rc3_strict = cli.main(["--home", str(home), "doctor", "--strict"])
    capsys.readouterr()
    assert rc3_strict == 0


def test_user_settings_path_injectable_never_reads_real_home(tmp_path, monkeypatch):
    """AC5: cfg.user_settings_path and MAESTRO_USER_SETTINGS_PATH both override
    the default ~/.claude/settings.json -- env wins over config."""
    monkeypatch.delenv("MAESTRO_USER_SETTINGS_PATH", raising=False)
    cfg_path = tmp_path / "cfg-settings.json"
    cfg = Config(home=tmp_path, user_settings_path=str(cfg_path))
    assert health.user_settings_path(cfg) == cfg_path

    env_path = tmp_path / "env-settings.json"
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(env_path))
    assert health.user_settings_path(cfg) == env_path


# --- RB-8: an unset daily_spend_ceiling_usd is a visible doctor warning -------


def test_doctor_warns_on_unset_spend_ceiling(home):
    """AC1: `maestro doctor` reports an unset ceiling as a named check with a
    warning status and today's spend for context, over the real CLI."""
    code, out = _sweep(home)
    assert code == 0  # a warning must not itself fail the (non-strict) sweep
    check = next(c for c in out["checks"] if c["name"] == "daily_spend")
    assert check["status"] == "warn"
    assert check["ceiling_usd"] is None
    assert check["today_usd"] == 0.0
    assert "uncapped" in check["detail"]


def test_doctor_passes_when_spend_ceiling_set(home):
    """AC2: with a ceiling configured, the check passes (not warn/fail) and the
    reported value matches the configured one, over the real CLI."""
    store.atomic_write(home / "config.toml", "[maestro]\ndaily_spend_ceiling_usd = 25.0\n")
    code, out = _sweep(home)
    assert code == 0
    check = next(c for c in out["checks"] if c["name"] == "daily_spend")
    assert check["status"] == "ok"
    assert check["ceiling_usd"] == 25.0


def test_check_daily_spend_unaffected_for_unavailable_meter(home, cfg):
    """The pre-existing unavailable-meter branch (text session log format) is
    untouched by the RB-8 unset-ceiling warning -- still its own warn/detail."""
    cfg.session_log_format = "text"
    result = health.check_daily_spend(cfg, store.now_epoch())
    assert result["status"] == "warn"
    assert "unavailable" in result["detail"]
    assert "uncapped" not in result["detail"]


def test_init_sets_a_nonzero_default_spend_ceiling(tmp_path):
    """AC4: a home created by `maestro init` is not silently uncapped -- the
    generated config.toml carries an actual `daily_spend_ceiling_usd` (chosen
    over a commented-out recommendation: only a real default protects an
    operator who never reads the file -- see DEFAULT_CONFIG_TOML's own comment
    for the dogfood-board justification). Doctor on that same fresh home does
    not warn on this check, proving the default actually closes the gap."""
    from maestro.config import load

    assert cli.main(["--home", str(tmp_path), "init"]) == 0
    cfg = load(str(tmp_path))
    assert cfg.daily_spend_ceiling_usd is not None
    assert cfg.daily_spend_ceiling_usd > 0

    code, out = _sweep(tmp_path)
    assert code == 0
    check = next(c for c in out["checks"] if c["name"] == "daily_spend")
    assert check["status"] == "ok"
