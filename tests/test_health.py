import io
import json
import os
import subprocess
import sys

from maestro import backup, cli, claims, dispatcher as disp, health, ops, store
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
    the 2026-07-19 cadence (~11s) exceed the derived budget and doctor trips.

    RF-7 retopologized the variable-weight phase from `implementing` (which
    used to fan out an unbounded, invisible-to-the-ledger `Agent`-tool QA loop
    -- up to `1 + max_impl_turns` agent-equivalents per spawn) to `qa` (which
    fans out at most ONE Standards-axis sub-agent, config-gated, so at most
    weight 2 -- see `dispatcher.spawn_weight`). The implement<->qa ping-pong is
    now made of REAL, separately-counted dispatcher spawns instead of one
    spawn hiding an unbounded fan-out, so this replay uses `qa` tickets with
    the Standards axis on to keep exercising a genuinely weighted phase; the
    bare, un-throttled spawn rate alone (weight 1) is what test_dispatcher.py's
    own runaway-brake tests already cover.
    """
    for k in ("T-1", "T-2", "T-3", "T-4"):
        _seed(home, k, Phase.QA)
    cfg = Config(home=home, max_concurrency=4, min_spawn_interval=0, qa_standards_axis=True)
    sessions = _EphemeralSessions()
    t0, step = store.now_epoch(), 11
    for i in range(12):
        disp.dispatch(cfg, sessions, now=t0 + i * step)
    code, out = _sweep(home)
    assert out["runaway"] is True and code == 1

    for i in range(12, 100):  # ~18 minutes of 11s sweeps, well inside the 1h window
        disp.dispatch(cfg, sessions, now=t0 + i * step)
    code, out = _sweep(home)
    assert out["runaway"] is True
    assert code == 1


def test_same_tickets_under_default_floor_do_not_trip(home):
    """The same shape, but with the default spawn floor engaged, stays
    healthy -- the GA-14 central-design-trap check: with the floor respected,
    each of the 4 `qa` tickets spawns only ~4 times in this window, weighted
    rate 16 * W comfortably under the phase-aware budget (health.spawn_budget),
    which is NOT silently redefined down to a bare session count just because
    every ticket happens to be `qa` with the Standards axis on."""
    for k in ("T-1", "T-2", "T-3", "T-4"):
        _seed(home, k, Phase.QA)
    cfg = Config(home=home, max_concurrency=4, qa_standards_axis=True)  # min_spawn_interval
                                                 # defaults to reconcile_steady_interval (300)
    sessions = _EphemeralSessions()
    t0, step = store.now_epoch(), 11
    for i in range(100):
        disp.dispatch(cfg, sessions, now=t0 + i * step)

    W_qa = disp.spawn_weight(cfg, Phase.QA.value)  # 1 + 1 (Standards axis on) == 2
    assert health.spawn_rate(home, t0 + 99 * step)["total"] == 16 * W_qa
    code, out = _sweep(home)
    assert out["runaway"] is False
    assert code == 0


def test_implementing_no_longer_carries_variable_weight(home):
    """RF-7's core weighting claim, asserted directly: an `implementing` spawn
    is now an ordinary, fixed-weight session (never a preventive multiplier)
    regardless of `max_impl_turns` or `qa_standards_axis` -- the ping-pong that
    used to hide behind that estimate is now real, separately-counted `qa`
    spawns (see test_qa_roundtrip.py)."""
    cfg_default = Config(home=home)
    cfg_standards = Config(home=home, qa_standards_axis=True)
    assert disp.spawn_weight(cfg_default, Phase.IMPLEMENTING.value) == 1
    assert disp.spawn_weight(cfg_standards, Phase.IMPLEMENTING.value) == 1
    assert disp.spawn_weight(cfg_default, Phase.QA.value) == 1
    assert disp.spawn_weight(cfg_standards, Phase.QA.value) == 2


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


def test_check_backup_age_never_swept_is_ok_not_warn(home, cfg):
    """T-88 AC3: a freshly-initialized home with no backups and no dispatcher
    sweep (no derived/.heartbeat.json) reports its own non-warning state,
    mirroring check_heartbeat's "no heartbeat yet" -> status ok contract --
    not the noisy `warn` a never-swept board used to get."""
    cfg.backup_interval = 100
    assert not (home / "derived" / ".heartbeat.json").exists()
    result = health.check_backup_age(cfg, store.now_epoch())
    assert result["status"] == "ok"
    assert result["age_s"] is None
    assert "never swept" in result["detail"]


def test_check_backup_age_warns_when_swept_and_no_backups_exist(home, cfg):
    """A board that HAS swept (has a heartbeat) but has no tarballs at all is a
    real fault, distinct from the never-swept case above."""
    cfg.backup_interval = 100
    store.write_json(home / "derived" / ".heartbeat.json",
                     {"ts": "x", "epoch": store.now_epoch(), "spawned": 0})
    result = health.check_backup_age(cfg, store.now_epoch())
    assert result["status"] == "warn"
    assert result["age_s"] is None


def test_check_backup_age_ignores_a_stale_cursor_over_an_emptied_backup_dir(home, cfg):
    """T-88 AC2: grounded in the tarballs, not the cursor -- a fresh cursor
    epoch over an empty backup_dir must NOT read as ok."""
    cfg.backup_interval = 100
    now = store.now_epoch()
    store.write_json(home / "derived" / ".heartbeat.json", {"ts": "x", "epoch": now, "spawned": 0})
    store.write_json(home / "derived" / ".backup_cursor.json", {"epoch": now})
    assert not backup.resolve_backup_dir(cfg).exists()
    result = health.check_backup_age(cfg, now)
    assert result["status"] != "ok"


def test_check_backup_age_warns_when_newest_tarball_is_stale(home, cfg):
    cfg.backup_interval = 100
    now = float(int(store.now_epoch()))  # whole seconds: the tarball name is second-precision
    backup.create_backup(cfg, now - 1000)
    result = health.check_backup_age(cfg, now)
    assert result["status"] == "warn"
    assert result["age_s"] == 1000


def test_check_backup_age_ok_when_newest_tarball_is_fresh(home, cfg):
    cfg.backup_interval = 3600
    now = float(int(store.now_epoch()))  # whole seconds: the tarball name is second-precision
    backup.create_backup(cfg, now - 10)
    result = health.check_backup_age(cfg, now)
    assert result["status"] == "ok"
    assert result["age_s"] == 10


def test_check_backup_age_uses_the_newest_of_several_tarballs(home, cfg):
    """A stale cursor (or none at all) must not shadow a newer tarball that a
    manual `maestro backup` created after the dispatcher's last sweep."""
    cfg.backup_interval = 3600
    now = float(int(store.now_epoch()))  # whole seconds: the tarball name is second-precision
    backup.create_backup(cfg, now - 5000)
    backup.create_backup(cfg, now - 5)
    result = health.check_backup_age(cfg, now)
    assert result["status"] == "ok"
    assert result["age_s"] == 5


def test_check_backup_age_ok_via_real_cli_init_backup_doctor_strict(tmp_path, capsys):
    """T-88 AC1: `init` -> `backup` -> `doctor --strict` over a temp home, driven
    entirely through the real CLI (never a hand-written cursor) -- and exits 0
    with `backup_age` reporting `status == "ok"`, matching the spec's own
    reproduction (a fresh backup_interval-armed home used to WARN "no backup
    yet" here despite the tarball `backup` just wrote)."""
    home = tmp_path / "home"
    assert cli.main(["--home", str(home), "init"]) == 0
    capsys.readouterr()
    assert cli.main(["--home", str(home), "backup"]) == 0
    capsys.readouterr()
    rc = cli.main(["--home", str(home), "doctor", "--strict"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    check = next(c for c in out["checks"] if c["name"] == "backup_age")
    assert check["status"] == "ok"


def test_check_backup_age_ok_after_dispatcher_sweep_runs_maybe_backup(home, cfg):
    """T-88 AC6: the existing dispatcher-timer path (`backup.maybe_backup`) is
    not regressed -- a real sweep still leaves `backup_age` at status "ok"."""
    _seed(home, "T-1", Phase.READY)
    now = store.now_epoch()
    disp.dispatch(cfg, DryRunSessions(), now=now)
    assert backup.list_backups(cfg)  # maybe_backup actually ran
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


# --- watchdog_loops check (AC7, OC-7/T-65) --------------------------------------


def test_check_watchdog_loops_warns_on_repeated_identical_failure(home, cfg):
    """AC7: a key that has tripped `_allow_spawn`'s no-progress watchdog more
    than once is surfaced by `maestro doctor` -- the exact shape of the
    2026-08-14 degraded-respawn-forever incident, visible without reading raw
    session logs."""
    from maestro import event_log

    _seed(home, "T-1", Phase.DEGRADED)
    for seq in (3, 8):
        event_log.append(home, "T-1", "Failed",
                         {"error": f"watchdog: 2 spawns with no progress at seq {seq}"},
                         actor="dispatcher")
    result = health.check_watchdog_loops(cfg, store.now_epoch())
    assert result["status"] == "warn"
    assert result["counts"] == {"T-1": 2}


def test_check_watchdog_loops_ok_below_threshold(home, cfg):
    """A single trip is not (yet) a loop -- only repetition is the signal."""
    from maestro import event_log

    _seed(home, "T-1", Phase.DEGRADED)
    event_log.append(home, "T-1", "Failed",
                     {"error": "watchdog: 5 spawns with no progress at seq 0"},
                     actor="dispatcher")
    result = health.check_watchdog_loops(cfg, store.now_epoch())
    assert result == {"name": "watchdog_loops", "status": "ok", "detail": "none", "counts": {}}


def test_check_watchdog_loops_ignores_unrelated_failures(home, cfg):
    """A non-watchdog `Failed` (e.g. a genuine implementation error) never
    counts toward this check, no matter how many times it recurs."""
    from maestro import event_log

    _seed(home, "T-1", Phase.DEGRADED)
    for _ in range(3):
        event_log.append(home, "T-1", "Failed", {"error": "tests still red"}, actor="reconciler")
    result = health.check_watchdog_loops(cfg, store.now_epoch())
    assert result["status"] == "ok"
    assert result["counts"] == {}


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
                      "phantom_keys", "watchdog_loops", "depends_on", "launchctl", "repo_preflight",
                      "unknown_repo_bindings", "language_binding", "missing_reconcile_skill",
                      "reconciler_permissions", "spawn_floor", "daily_spend", "burn",
                      "gh_credential_reachability", "ollama_models", "pi_models", "runner_binary",
                      "pi_version", "worktree_health", "provider_availability", "missing_acs"}
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
    grew it by one more -- `ollama_models` -- same treatment. T-38 (OC-2) grew
    it by one more still -- `runner_binary` -- same treatment. MTO-1 grew it by one
    more still -- `worktree_health` -- same treatment. MTO-8 grew it by one more
    still -- `provider_availability` -- same treatment. RB-11 grew it by one more
    still -- `burn` -- same treatment. T-65 (OC-7) grew it by one more still --
    `watchdog_loops` -- same treatment. T-56 (PI-4) grew it by one more still --
    `pi_version` -- same treatment. T-61 (PI-9) grew it by one more still --
    `pi_models` -- same treatment. T-77 (RB-17) grew it by one more still --
    `phantom_keys` -- same treatment. T-80 grew it by one more still --
    `missing_acs` -- same treatment. T-96 grew it by one more still --
    `language_binding` -- same treatment.)"""
    baseline_names = {
        "heartbeat", "backup_age", "claim_age", "claim_no_output", "dead_letters",
        "phantom_keys", "watchdog_loops", "depends_on", "repo_preflight", "unknown_repo_bindings",
        "language_binding", "missing_reconcile_skill", "reconciler_permissions", "spawn_floor",
        "daily_spend", "gh_credential_reachability", "launchctl", "ollama_models",
        "pi_models", "runner_binary", "pi_version", "worktree_health",
        "provider_availability", "burn", "missing_acs",
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
    assertion below is about reconciler_permissions specifically -- T-87's
    per-file completeness rule needs the FULL PAYLOAD_NAMES set installed, not
    just one file, or this check itself would warn and confound the --strict
    assertion this helper exists to isolate."""
    from maestro import skills_install
    d = repo / ".claude" / "commands"
    d.mkdir(parents=True, exist_ok=True)
    for name in skills_install.PAYLOAD_NAMES:
        (d / name).write_text("# stub\n")


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


def test_reconciler_permissions_emitted_maestro_grant_satisfies_check(home, tmp_path, monkeypatch):
    """MTO-5 AC1: the narrow per-verb form (never the bare wildcard,
    test_bare_wildcard_never_appears) satisfies the 'maestro' portion of this
    check. The check and the spawner now agree on what 'granted' means,
    instead of the check demanding a literal (Bash(maestro:*)) the spawner is
    tested to never emit.

    RB-16: a real spawn's own --allowedTools is phase-narrowed now
    (dispatcher.phase_verb_grant), so it's no longer the right thing to check
    a repo's static settings.json against -- a repo's settings can't vary by
    phase the way a spawn's argv does. `dispatcher.maestro_verb_grant()`
    (its default, the full AGENT_TOOL_VERBS ceiling) is: the union of every
    verb ANY phase could ever request, exactly what `_missing_maestro_grant`
    checks for (see its own docstring)."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    cfg = Config(home=home, repo_path=str(repo), reconcile_web_tools=False)
    emitted = disp.maestro_verb_grant()          # the full per-phase-union ceiling
    assert "Bash(maestro:*)" not in emitted      # never the bare wildcard (AD-1)
    _write_repo_settings(repo, allow=emitted + ["Bash(git:*)", "Bash(gh:*)", "Bash(python3:*)"])

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "ok"
    assert check["missing_by_repo"] == {}


def test_reconciler_permissions_ok_without_venv_for_non_self_repo(home, tmp_path, monkeypatch):
    """MTO-5 AC2: a bound repo with no .venv/bin/ (a yarn or Bazel toolchain)
    reaches an ok reconciler_permissions check -- Bash(.venv/bin/:*) is not
    board-wide required, only for the binding that IS maestro's own repo (see
    test_reconciler_permissions_venv_required_only_for_own_repo below)."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "web-ui"
    _init_plain_repo(repo)
    assert "Bash(.venv/bin/:*)" not in disp.RECONCILER_REQUIRED_TOOLS
    _write_repo_settings(repo, allow=list(disp.RECONCILER_REQUIRED_TOOLS))
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "ok"


def test_reconciler_permissions_venv_required_only_for_own_repo(home, tmp_path, monkeypatch):
    """MTO-5: Bash(.venv/bin/:*) IS required of the binding that resolves to
    maestro's own repo checkout -- simulated via monkeypatching
    health._maestro_self_root so the test doesn't depend on where this test
    process itself happens to be installed from."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    monkeypatch.setattr(health, "_maestro_self_root", lambda: repo.resolve())
    _write_repo_settings(repo, allow=list(disp.RECONCILER_REQUIRED_TOOLS))  # everything but venv
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "warn"
    assert check["missing_by_repo"]["default"] == ["Bash(.venv/bin/:*)"]

    _write_repo_settings(repo, allow=list(disp.RECONCILER_REQUIRED_TOOLS) +
                          list(disp.MAESTRO_OWN_REPO_EXTRA_TOOLS))
    checks2 = health.run_checks(cfg, 1000)
    check2 = next(c for c in checks2 if c["name"] == "reconciler_permissions")
    assert check2["status"] == "ok"


def test_reconciler_permissions_narrower_gh_grant_reaches_ok(home, tmp_path, monkeypatch):
    """T-94 AC1: Bash(gh pr:*) (covering every gh invocation the reconciler
    skills make -- gh pr view / gh pr create) in place of the coarse
    Bash(gh:*), plus the other required literals unchanged, reaches ok --
    the exact false positive from this ticket's own repro."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    allow = [t for t in disp.RECONCILER_REQUIRED_TOOLS if t != "Bash(gh:*)"] + ["Bash(gh pr:*)"]
    _write_repo_settings(repo, allow=allow)
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "ok"
    assert check["missing_by_repo"] == {}


def test_reconciler_permissions_narrower_git_python_venv_grants_reach_ok(home, tmp_path, monkeypatch):
    """T-94 AC2: the same coverage-aware acceptance holds for Bash(git:*),
    Bash(python3:*), and (on the binding _is_maestro_own_repo matches)
    Bash(.venv/bin/:*) -- each satisfied by its own narrower rule set
    instead of the coarse literal."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    monkeypatch.setattr(health, "_maestro_self_root", lambda: repo.resolve())
    allow = (["Bash(maestro:*)", "Bash(gh pr:*)", "Bash(python3 -m:*)", "Bash(.venv/bin/python:*)"] +
             list(disp.RECONCILER_LITERAL_COVERAGE["Bash(git:*)"]))
    _write_repo_settings(repo, allow=allow)
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "ok"
    assert check["missing_by_repo"] == {}


def test_reconciler_permissions_token_boundary_safety(home, tmp_path, monkeypatch):
    """T-94 AC3: a differently-spelled grant that merely shares a prefix --
    Bash(ghq:*) or Bash(github:*) in place of Bash(gh:*) -- does NOT satisfy
    the requirement; the check still WARNs and still names Bash(gh:*)
    missing."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    allow = ([t for t in disp.RECONCILER_REQUIRED_TOOLS if t != "Bash(gh:*)"] +
             ["Bash(ghq:*)", "Bash(github:*)"])
    _write_repo_settings(repo, allow=allow)
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "warn"
    assert "Bash(gh:*)" in check["missing_by_repo"]["default"]


def test_reconciler_permissions_partial_gh_subset_still_warns(home, tmp_path, monkeypatch):
    """T-94 AC4 (no false negative): a strict subset of the gh coverage --
    only Bash(gh api:*), with no Bash(gh pr:*) -- still WARNs and still
    names Bash(gh:*) missing, exactly like before this ticket."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    allow = [t for t in disp.RECONCILER_REQUIRED_TOOLS if t != "Bash(gh:*)"] + ["Bash(gh api:*)"]
    _write_repo_settings(repo, allow=allow)
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "warn"
    assert "Bash(gh:*)" in check["missing_by_repo"]["default"]


def _assert_deny_defeats_narrower_gh_coverage(home, tmp_path, monkeypatch, deny):
    """Shared body for T-94 AC5's two deny variants below."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    allow = [t for t in disp.RECONCILER_REQUIRED_TOOLS if t != "Bash(gh:*)"] + ["Bash(gh pr:*)"]
    _write_repo_settings(repo, allow=allow, deny=deny)
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "warn"
    assert "Bash(gh:*)" in check["missing_by_repo"]["default"]
    assert "denied by settings: Bash(gh:*)" in check["detail"]


def test_reconciler_permissions_deny_of_narrower_grant_defeats_coverage(home, tmp_path, monkeypatch):
    """T-94 AC5: deny still wins over the coverage-aware acceptance -- an
    allow of Bash(gh pr:*) together with a deny of that same narrower rule
    still reports Bash(gh:*) missing and lists it under denied_by_repo."""
    _assert_deny_defeats_narrower_gh_coverage(home, tmp_path, monkeypatch, ["Bash(gh pr:*)"])


def test_reconciler_permissions_deny_of_coarse_literal_defeats_coverage(home, tmp_path, monkeypatch):
    """T-94 AC5: same, but the deny targets the coarse Bash(gh:*) the
    narrower Bash(gh pr:*) grant would otherwise cover."""
    _assert_deny_defeats_narrower_gh_coverage(home, tmp_path, monkeypatch, ["Bash(gh:*)"])


def test_reconciler_permissions_detail_no_longer_claims_exact_string(home, tmp_path, monkeypatch):
    """T-94 AC6: the WARN detail no longer claims exact-string matching --
    that sentence is gone, replaced by wording that describes the new
    coverage-aware acceptance."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    _write_repo_settings(repo, allow=["Bash(maestro:*)"])
    cfg = Config(home=home, repo_path=str(repo))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "warn"
    assert "exact-string" not in check["detail"]
    assert "narrower grant" in check["detail"]


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
    # T-88: backup_interval is left at its default (3600, armed) rather than
    # disabled -- the never-swept board (no derived/.heartbeat.json here) reads
    # as backup_age status "ok" on its own now, so this exercises --strict with
    # backups actually armed instead of sidestepping that check entirely.
    (home / "config.toml").write_text(
        f'[maestro]\ndaily_spend_ceiling_usd = 50.0\n\n'
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


def test_doctor_strict_exits_0_with_narrower_gh_pr_style_grant(home, tmp_path, monkeypatch, capsys):
    """T-94 AC7: `maestro doctor --strict` exits 0 over a temp MAESTRO_HOME
    whose bound repo grants the maestro verb set plus only the narrower
    Bash(gh pr:*)-style rules (never the coarse Bash(gh:*)) -- driven through
    the real CLI -- and still exits 1 while a genuine gap (no gh coverage at
    all) remains."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    _install_dummy_reconcile_skill(repo)
    (home / "config.toml").write_text(
        f'[maestro]\ndaily_spend_ceiling_usd = 50.0\n\n'
        f'[repos.alpha]\npath = "{repo}"\ndefault = true\n')
    _seed_bound_ticket(home, "T-1", "alpha")

    allow = [t for t in disp.RECONCILER_REQUIRED_TOOLS if t != "Bash(gh:*)"] + ["Bash(gh pr:*)"]
    _write_repo_settings(repo, allow=allow)

    rc = cli.main(["--home", str(home), "doctor"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    check = next(c for c in out["checks"] if c["name"] == "reconciler_permissions")
    assert check["status"] == "ok"

    rc_strict = cli.main(["--home", str(home), "doctor", "--strict"])
    capsys.readouterr()
    assert rc_strict == 0

    # a genuine gap -- no gh coverage of any kind -- still exits 1.
    _write_repo_settings(repo, allow=[t for t in disp.RECONCILER_REQUIRED_TOOLS if t != "Bash(gh:*)"])
    rc2 = cli.main(["--home", str(home), "doctor"])
    out2 = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    check2 = next(c for c in out2["checks"] if c["name"] == "reconciler_permissions")
    assert check2["status"] == "warn"

    rc2_strict = cli.main(["--home", str(home), "doctor", "--strict"])
    capsys.readouterr()
    assert rc2_strict == 1


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


# --- OC-1: both checks become runner-aware instead of silently checking the
# wrong (Claude-only) surface for a non-claude-runner ticket -------------------

from maestro import event_log as event_log_mod  # noqa: E402
from maestro import snapshot as snap_mod  # noqa: E402


def _seed_with_runner(home, key, *, phase=Phase.IMPLEMENTING, runner=None, tier=0):
    extra = f"runner: {runner}\n" if runner else ""
    spec = f"# {key}\napproval_tier: {tier}\n{extra}dependsOn: []\n"
    store.atomic_write(store.spec_path(home, key), spec)
    event_log_mod.append(home, key, "TicketCreated",
                          {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log_mod.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def test_missing_reconcile_skill_checks_opencode_location_for_non_claude_runner(home, tmp_path, monkeypatch):
    """AC2: a repo bound by an opencode-runner ticket is checked against
    opencode's OWN command location, not `.claude/commands/` -- warn while
    absent, ok once present."""
    monkeypatch.setenv("MAESTRO_OPENCODE_COMMANDS_DIR", str(tmp_path / "no-opencode-user-commands"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    _seed_with_runner(home, "T-1", runner="opencode")

    check = health.check_missing_reconcile_skill(cfg, 1000)
    assert check["status"] == "warn"
    assert "default" in check["missing"]

    # T-87: the completeness rule needs the FULL PAYLOAD_NAMES set, not one file.
    from maestro import skills_install
    command_dir = repo / ".opencode" / "command"
    command_dir.mkdir(parents=True)
    for name in skills_install.PAYLOAD_NAMES:
        (command_dir / name).write_text("# stub\n")

    check = health.check_missing_reconcile_skill(cfg, 1000)
    assert check["status"] == "ok"
    assert check["missing"] == []


def test_missing_reconcile_skill_claude_side_install_alone_does_not_satisfy_opencode_runner(
        home, tmp_path, monkeypatch):
    """Checking `.claude/commands/` for an opencode-runner ticket would be a
    check of the wrong file -- a Claude-side install alone must NOT read ok."""
    monkeypatch.setenv("MAESTRO_OPENCODE_COMMANDS_DIR", str(tmp_path / "no-opencode-user-commands"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".claude" / "commands").mkdir(parents=True)
    (repo / ".claude" / "commands" / "maestro-reconcile-implementing.md").write_text("# stub\n")
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    _seed_with_runner(home, "T-1", runner="opencode")

    check = health.check_missing_reconcile_skill(cfg, 1000)
    assert check["status"] == "warn"
    assert "default" in check["missing"]


def test_doctor_cli_missing_reconcile_skill_warn_then_ok_for_opencode_runner_ticket(
        home, tmp_path, capsys, monkeypatch):
    """Same AC2 contract, proven over the real `maestro doctor --json` CLI."""
    monkeypatch.setenv("MAESTRO_OPENCODE_COMMANDS_DIR", str(tmp_path / "no-opencode-user-commands"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{repo}"\nmin_spawn_interval = 0\n')
    _seed_with_runner(home, "T-1", runner="opencode")

    assert cli.main(["--home", str(home), "doctor"]) == 0
    warn_check = next(c for c in json.loads(capsys.readouterr().out)["checks"]
                       if c["name"] == "missing_reconcile_skill")
    assert warn_check["status"] == "warn"

    # T-87: the completeness rule needs the FULL PAYLOAD_NAMES set, not one file.
    from maestro import skills_install
    command_dir = repo / ".opencode" / "command"
    command_dir.mkdir(parents=True)
    for name in skills_install.PAYLOAD_NAMES:
        (command_dir / name).write_text("# stub\n")

    assert cli.main(["--home", str(home), "doctor"]) == 0
    ok_check = next(c for c in json.loads(capsys.readouterr().out)["checks"]
                     if c["name"] == "missing_reconcile_skill")
    assert ok_check["status"] == "ok"


# --- T-61 (PI-9): missing_reconcile_skill treats a pi binding as always satisfied ---

def test_missing_reconcile_skill_pi_binding_always_ok_with_neither_location_present(
        home, tmp_path, monkeypatch):
    """AC5: a real doctor run over a board with a pi ticket and NEITHER
    `.claude/commands/` nor `.opencode/command/` present still reports `ok`
    -- PI-8 passes `--prompt-template` at the package payload directly, so
    there is no per-repo command-file location for pi to be missing from."""
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(tmp_path / "no-user-commands"))
    monkeypatch.setenv("MAESTRO_OPENCODE_COMMANDS_DIR", str(tmp_path / "no-opencode-user-commands"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    cfg.provider_config = {"runner": {"pi": {"phases": ["implementing"]}}}
    _seed_with_runner(home, "T-1", runner="pi")

    check = health.check_missing_reconcile_skill(cfg, 1000)
    assert check["status"] == "ok"
    assert check["missing"] == []


# --- T-87: per-file completeness, not any()-over-glob --------------------------

def _install_partial_payload(commands_dir, *, missing=("maestro-reconcile-qa.md",)):
    """Writes every `skills_install.PAYLOAD_NAMES` file EXCEPT *missing* into
    *commands_dir* -- the "6 of 7" real-board-install shape this ticket is
    named for."""
    from maestro import skills_install
    commands_dir.mkdir(parents=True, exist_ok=True)
    for name in skills_install.PAYLOAD_NAMES:
        if name in missing:
            continue
        (commands_dir / name).write_text("# stub\n")


def test_missing_reconcile_skill_partial_install_warns_naming_the_missing_file(
        home, tmp_path, monkeypatch):
    """AC1 + AC2: 6 of the 7 phase files present (missing `qa`), user-scope
    empty -- the check must warn and name `maestro-reconcile-qa.md` verbatim,
    not read `ok` because SOME file exists (the any()-over-glob defect)."""
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(tmp_path / "no-user-commands"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    _install_partial_payload(repo / ".claude" / "commands")
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    _seed(home, "T-1", Phase.IMPLEMENTING)

    check = health.check_missing_reconcile_skill(cfg, 1000)
    assert check["status"] == "warn"
    assert "default" in check["missing"]
    assert check["missing_files"]["default"] == ["maestro-reconcile-qa.md"]
    assert "maestro-reconcile-qa.md" in check["detail"]
    # the other 6 files being present must not leak into "missing"
    assert "maestro-reconcile-triaging.md" not in check["detail"]


def test_doctor_strict_gates_on_partial_reconcile_skill_install(home, tmp_path, monkeypatch, capsys):
    """AC3: `maestro doctor --strict` exits 1 while the bound repo carries only
    6 of the 7 phase files and 0 once the seventh is written, with every other
    check already ok -- driven through the real CLI."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(tmp_path / "no-user-commands"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    commands_dir = repo / ".claude" / "commands"
    _install_partial_payload(commands_dir)
    _write_repo_settings(repo, allow=list(disp.RECONCILER_REQUIRED_TOOLS))
    (home / "config.toml").write_text(
        f'[maestro]\nbackup_interval = 0\ndaily_spend_ceiling_usd = 50.0\n\n'
        f'[repos.alpha]\npath = "{repo}"\ndefault = true\n')
    _seed_bound_ticket(home, "T-1", "alpha")

    rc = cli.main(["--home", str(home), "doctor"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    check = next(c for c in out["checks"] if c["name"] == "missing_reconcile_skill")
    assert check["status"] == "warn"

    rc_strict = cli.main(["--home", str(home), "doctor", "--strict"])
    capsys.readouterr()
    assert rc_strict == 1

    (commands_dir / "maestro-reconcile-qa.md").write_text("# stub\n")

    rc2 = cli.main(["--home", str(home), "doctor"])
    out2 = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    check2 = next(c for c in out2["checks"] if c["name"] == "missing_reconcile_skill")
    assert check2["status"] == "ok"

    rc2_strict = cli.main(["--home", str(home), "doctor", "--strict"])
    capsys.readouterr()
    assert rc2_strict == 0


def test_missing_reconcile_skill_per_file_union_across_repo_and_user_scope(
        home, tmp_path, monkeypatch):
    """AC4: 6 files live in the repo's `.claude/commands/`, the 7th ONLY in the
    user-scope dir -- the union is per-file, so this reads `ok`. Neither
    directory alone is complete, proving "either directory is complete" is
    NOT the rule being applied."""
    user_dir = tmp_path / "user-commands"
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(user_dir))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    _install_partial_payload(repo / ".claude" / "commands")
    user_dir.mkdir(parents=True)
    (user_dir / "maestro-reconcile-qa.md").write_text("# stub\n")
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    _seed(home, "T-1", Phase.IMPLEMENTING)

    check = health.check_missing_reconcile_skill(cfg, 1000)
    assert check["status"] == "ok"
    assert check["missing"] == []


def test_missing_reconcile_skill_stray_user_scope_file_does_not_suppress_board_wide(
        home, tmp_path, monkeypatch):
    """AC5: a single stray `maestro-reconcile-*.md` in the user-scope dir (not
    the phase actually missing repo-side) must not suppress the check -- the
    old any()-per-directory rule read a non-empty user dir as "user-scope is
    complete" regardless of WHICH file it held."""
    user_dir = tmp_path / "user-commands"
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(user_dir))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    _install_partial_payload(repo / ".claude" / "commands")  # missing qa
    user_dir.mkdir(parents=True)
    (user_dir / "maestro-reconcile-triaging.md").write_text("# stray, already present repo-side\n")
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    _seed(home, "T-1", Phase.IMPLEMENTING)

    check = health.check_missing_reconcile_skill(cfg, 1000)
    assert check["status"] == "warn"
    assert check["missing_files"]["default"] == ["maestro-reconcile-qa.md"]


def test_reconciler_permissions_not_applicable_for_an_unregistered_runner(home, tmp_path, monkeypatch):
    """A repo bound by a ticket naming a runner this check has no dedicated
    branch for (not ``claude``, not ``opencode``) is reported
    `not_applicable_by_repo` with a reason naming the runner -- this check must
    never fold a surface it did not inspect into a bare ok/warn. Uses a made-up
    runner name (`resolve_runner` reads the spec's `runner:` line verbatim, with
    no registration check of its own) rather than "opencode", which T-64 gave
    its own dedicated, actively-checked branch -- see the tests below."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    _seed_with_runner(home, "T-1", runner="some-future-runner")

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["missing_by_repo"] == {}          # nothing CLAUDE-scoped was found missing
    assert "default" in check["not_applicable_by_repo"]
    assert "some-future-runner" in check["not_applicable_by_repo"]["default"]


def test_reconciler_permissions_warns_for_opencode_runner_missing_config(home, tmp_path, monkeypatch):
    """T-64: unlike a genuinely-unregistered runner (above), an opencode-runner
    binding now gets its OWN real inspection -- the generated opencode.jsonc --
    instead of being folded into `not_applicable_by_repo` (the exact silent-`ok`
    class QW-1 exists to prevent: T-64's Notes cite this check reading `ok` for
    an opencode-runner ticket while checking nothing)."""
    monkeypatch.setenv("MAESTRO_OPENCODE_COMMANDS_DIR", str(tmp_path / "no-opencode-user-commands"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0,
                 runner_enabled=["claude", "opencode"])
    _seed_with_runner(home, "T-1", runner="opencode")

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["not_applicable_by_repo"] == {}
    assert "default" in check["missing_by_repo"]

    from maestro import skills_install
    skills_install.install_repo(cfg, "default")

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["missing_by_repo"] == {}


def test_reconciler_permissions_claude_runner_ticket_unaffected_by_opencode_check(home, tmp_path, monkeypatch):
    """A claude-runner ticket's binding still gets the full Claude Code
    settings.json inspection -- OC-1 doesn't weaken the existing check."""
    monkeypatch.setenv("MAESTRO_USER_SETTINGS_PATH", str(tmp_path / "no-user-settings.json"))
    repo = tmp_path / "repo"
    _init_plain_repo(repo)
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    _seed_with_runner(home, "T-1", runner=None)   # default runner: claude

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "warn"
    assert "default" in check["missing_by_repo"]
    assert check["not_applicable_by_repo"] == {}


# --- T-61 (PI-9) AC6: a pi binding never reports `ok` with no guard extension --

def test_reconciler_permissions_warns_for_pi_binding_with_no_guard_extension_installed(
        home, tmp_path):
    """Before T-61, a pi binding fell into `not_applicable_by_repo` and
    `status` stayed `ok` even though pi ships NO permission gate of its own
    -- an ungated pi board reading a green doctor is the worst possible
    output this check exists to prevent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    cfg.provider_config = {"runner": {"pi": {"phases": ["implementing"]}}}
    _seed_with_runner(home, "T-1", runner="pi")

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "warn"
    assert "default" in check["missing_by_repo"]
    assert check["not_applicable_by_repo"] == {}
    assert "not installed" in check["missing_by_repo"]["default"][0]


def test_reconciler_permissions_ok_for_pi_binding_once_guard_extension_is_installed(
        home, tmp_path):
    """Once `pi_guard.install` has actually materialized the extension (a
    real prior pi spawn's own belt-and-suspenders call, PI-7) -- under a
    per-KEY subdirectory of `store.pi_agent_dir(cfg.home)` (RB-16 fix round --
    see `store.pi_agent_key_dir`'s docstring), the same directory
    `_pi_permission_gap` now scans -- the check reports `ok`."""
    from maestro import pi_guard

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    cfg.provider_config = {"runner": {"pi": {"phases": ["implementing"]}}}
    _seed_with_runner(home, "T-1", runner="pi")
    pi_guard.install(store.pi_agent_key_dir(store.pi_agent_dir(home), "T-1"))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "ok"
    assert check["missing_by_repo"] == {}


def test_reconciler_permissions_pi_never_reads_claude_code_settings(home, tmp_path):
    """A pi binding's Claude Code settings.json (allow/deny) is irrelevant --
    the guard-extension probe never even looks at it, unlike a claude
    binding."""
    repo = tmp_path / "repo"
    _init_plain_repo(repo)  # deliberately no .claude/settings*.json at all
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0)
    cfg.provider_config = {"runner": {"pi": {"phases": ["implementing"]}}}
    _seed_with_runner(home, "T-1", runner="pi")
    from maestro import pi_guard
    pi_guard.install(store.pi_agent_key_dir(store.pi_agent_dir(home), "T-1"))

    checks = health.run_checks(cfg, 1000)
    check = next(c for c in checks if c["name"] == "reconciler_permissions")
    assert check["status"] == "ok"


# --- T-54 AC8: "the runner for key K" under phase-aware routing ---------------

def test_runner_for_key_treats_a_never_eligible_runner_as_claude(home):
    """`_runner_for_key` decides: a runner the spec names but whose
    eligible-phase set is empty (misconfigured -- e.g. `phases = []`) can
    never actually be spawned for this key, so it's equivalent to `claude`
    for the doctor checks' purposes."""
    cfg = Config(home=home, provider_config={"runner": {"opencode": {"phases": []}}})
    _seed_with_runner(home, "T-1", runner="opencode")

    assert health._runner_for_key(cfg, "T-1") == "claude"


def test_runner_for_key_returns_the_spec_runner_when_eligible_anywhere(home):
    """Admitted to `qa` only (not `implementing`, T-1's current phase) --
    `_runner_for_key` still names it, since the repo surface it gates on must
    be ready for WHENEVER that eligible phase's spawn happens, not just the
    ticket's current one."""
    cfg = Config(home=home, provider_config={"runner": {"opencode": {"phases": ["qa"]}}})
    _seed_with_runner(home, "T-1", runner="opencode", phase=Phase.IMPLEMENTING)

    assert health._runner_for_key(cfg, "T-1") == "opencode"


def test_real_doctor_over_a_mixed_phase_runner_board_reports_without_exception(
        home, tmp_path, monkeypatch):
    """AC8: a board with a claude ticket, an opencode ticket admitted only at
    `implementing`, and another admitted only at `qa` -- `maestro doctor`
    completes cleanly over all of them."""
    monkeypatch.setenv("MAESTRO_OPENCODE_COMMANDS_DIR", str(tmp_path / "no-opencode-user-commands"))
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config(home=home, repo_path=str(repo), min_spawn_interval=0,
                provider_config={"runner": {"opencode": {"phases": ["qa"]}}})
    _seed_with_runner(home, "CL-1", runner=None, phase=Phase.READY)
    _seed_with_runner(home, "IMPL-1", runner="opencode", phase=Phase.IMPLEMENTING)
    _seed_with_runner(home, "QA-1", runner="opencode", phase=Phase.QA)

    checks = health.run_checks(cfg, 1000)  # must not raise
    names = {c["name"] for c in checks}
    assert "missing_reconcile_skill" in names
    assert "reconciler_permissions" in names


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


# --- MTO-1: `maestro doctor` catches a broken worktree by hand ---------------


def test_check_worktree_health_ok_with_no_worktrees_dir(home):
    """AC6: the common case -- nothing under worktrees/ yet -- is a plain ok,
    never a warn just for being empty."""
    cfg = Config(home=home)
    check = health.check_worktree_health(cfg, store.now_epoch())
    assert check == {"name": "worktree_health", "status": "ok",
                      "detail": "all existing worktrees have a valid index and clean status",
                      "broken": []}


def test_check_worktree_health_warns_on_a_directory_with_no_index(home):
    """AC6: an existing worktree directory that was never a real git worktree
    (no index at all) -- the exact false-positive the pre-MTO-1 bare
    `wt.exists()` gate fell for -- is surfaced as a warn, naming the key and
    the reason."""
    wt_dir = home / "worktrees" / "B-1"
    wt_dir.mkdir(parents=True)
    (wt_dir / "stray.txt").write_text("not a real worktree\n")

    cfg = Config(home=home)
    check = health.check_worktree_health(cfg, store.now_epoch())
    assert check["status"] == "warn"
    assert check["broken"] == [{"key": "B-1", "reason": "no index"}]
    assert "B-1" in check["detail"] and "no index" in check["detail"]


def test_check_worktree_health_warns_on_a_mass_deletion_git_status(home, tmp_path):
    """AC6, over a real git worktree (not a fake stray directory): files the
    index references go missing on disk -- `git status` reports a mass
    deletion block, the 2026-08-10 incident's exact signature -- and the
    check flags it by key."""
    from maestro import config as config_mod

    from conftest import make_origin_and_repo as _make_origin_and_repo

    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    for i in range(60):
        (repo / f"file{i}.txt").write_text(f"content {i}\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add many files", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    (home / "config.toml").write_text(f'[maestro]\nrepo_path = "{repo}"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    store.atomic_write(store.spec_path(home, "B-2"), "# B-2\napproval_tier: 1\n\n## Intent\nx\n")

    ops.worktree_ensure(cfg, "B-2")
    wt = store.worktree_path(home, "B-2")
    for i in range(55):
        (wt / f"file{i}.txt").unlink()

    check = health.check_worktree_health(cfg, store.now_epoch())
    assert check["status"] == "warn"
    assert check["broken"] == [{"key": "B-2", "reason": "mass deletion in git status"}]


def test_check_worktree_health_registered_in_doctor(home):
    """AC6: `maestro doctor` runs this check as part of the real registry, not
    just as a standalone function."""
    assert health.check_worktree_health in health.CHECKS
    code, out = _sweep(home)
    assert code == 0
    assert next(c for c in out["checks"] if c["name"] == "worktree_health")["status"] == "ok"


# ---------------------------------------------------------------------------
# T-96 AC3: `check_language_binding` -- WARN for a repo binding whose
# `test_command` is set, `language` is unset, and the repo's own test surface
# doesn't look python (a `go.mod`/`package.json` at its root, or no `tests/`
# directory at all).
# ---------------------------------------------------------------------------

def test_check_language_binding_warns_on_a_go_repo_with_no_language_set(home, tmp_path):
    from conftest import make_origin_and_repo as _make_origin_and_repo

    _origin, repo = _make_origin_and_repo(tmp_path, name="target")
    (repo / "go.mod").write_text("module widget\n\ngo 1.21\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "go.mod", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    cfg = Config(home=home, repos={
        "default": {"path": str(repo), "default": True, "test_command": "go test ./..."},
    })
    result = health.check_language_binding(cfg, now=1_000_000)
    assert result["status"] == "warn"
    assert result["flagged"] == [{"repo": "default", "reason": "looks like go"}]
    assert "language" in result["detail"]
    assert "[repos.default]" in result["detail"]


def test_check_language_binding_ok_when_language_is_set(home, tmp_path):
    from conftest import make_origin_and_repo as _make_origin_and_repo

    _origin, repo = _make_origin_and_repo(tmp_path, name="target")
    (repo / "go.mod").write_text("module widget\n\ngo 1.21\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "go.mod", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    cfg = Config(home=home, repos={
        "default": {"path": str(repo), "default": True, "test_command": "go test ./...",
                   "language": "go"},
    })
    result = health.check_language_binding(cfg, now=1_000_000)
    assert result["status"] == "ok"
    assert result["flagged"] == []


def test_check_language_binding_ok_for_a_python_board(home, tmp_path):
    """AC7: a plain python repo (a `tests/` dir, no go.mod/package.json)
    never flags -- no false positive from the new check."""
    from conftest import make_origin_and_repo as _make_origin_and_repo

    _origin, repo = _make_origin_and_repo(tmp_path, name="target")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "tests/", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    cfg = Config(home=home, repos={
        "default": {"path": str(repo), "default": True, "test_command": "pytest -q"},
    })
    result = health.check_language_binding(cfg, now=1_000_000)
    assert result["status"] == "ok"
    assert result["flagged"] == []


def test_check_language_binding_registered_in_doctor(home):
    assert health.check_language_binding in health.CHECKS
    code, out = _sweep(home)
    assert code == 0
    assert next(c for c in out["checks"] if c["name"] == "language_binding")["status"] == "ok"
