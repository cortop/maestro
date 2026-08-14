"""RB-11: per-key burn visibility (doctor) + the optional rate cap that parks
a burning key. Every test drives the real surface -- `burn.report`/
`should_park` directly, a real `dispatch()` sweep, the real `maestro doctor`
CLI, or the real `projection.render`/`write` -- never a mocked burn module.
Only the `claude -p` spawn (DryRunSessions and its subclasses below) is
mocked, per CLAUDE.md.
"""
import io
import json
import sys

from maestro import burn, cli, dispatcher as disp, event_log, ops, projection, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from test_dispatcher import _EphemeralSessions, _seed
from test_spend import _result_record, _write_stream_log


def _sweep_doctor(home):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(home), "doctor"])
    finally:
        sys.stdout = old
    return code, json.loads(buf.getvalue())


def _spawn_and_seed_ledger(home, cfg, key, now):
    cfg.min_spawn_interval = 0
    _seed(home, key, Phase.READY)
    report = disp.dispatch(cfg, DryRunSessions(), now=now)
    assert key in report.spawned
    return report


# --- AC1: per-key spend over a window, alongside spawns_last_hour, plus the --
# --- no-progress-spawn-count flag --------------------------------------------

def test_doctor_json_reports_per_key_spend_alongside_spawns_last_hour(home, cfg):
    t0 = store.now_epoch()
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    _write_stream_log(home, "T-1", t0, [_result_record(3.25)])

    code, out = _sweep_doctor(home)
    assert code == 0
    assert out["spend_usd_by_key"]["T-1"] == 3.25
    assert "T-1" in out["spawns_last_hour"]["by_key"]
    names = {c["name"] for c in out["checks"]}
    assert "burn" in names


def test_no_progress_spawn_count_flags_burn(home, cfg):
    """A key respawned `burn_repeat_threshold` times at the SAME observed_seq
    (dispatcher's own `.spawn_attempts.json` ledger) is flagged, even with no
    Failed history and no spend at all."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    snap = snap_mod.load(home, "T-1")
    store.write_json(disp._spawn_attempts_path(home),
                     {"T-1": {"seq": snap.observed_seq, "count": cfg.burn_repeat_threshold}})

    rpt = burn.report(cfg, store.now_epoch())
    assert rpt["no_progress_by_key"]["T-1"] == cfg.burn_repeat_threshold
    assert "T-1" in rpt["flagged"]

    code, out = _sweep_doctor(home)
    assert code == 0
    assert "T-1" in out["burning_keys"]
    burn_check = next(c for c in out["checks"] if c["name"] == "burn")
    assert burn_check["status"] == "warn"


def test_stale_attempts_entry_from_before_progress_is_not_flagged(home, cfg):
    """AC4: `.spawn_attempts.json` is only reset lazily, on the key's NEXT
    spawn attempt -- a stale high-count entry recorded against an OLDER
    observed_seq than the key's live snapshot must not be trusted, since real
    progress may have happened since it was written."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    stale_seq = snap_mod.load(home, "T-1").observed_seq
    event_log.append(home, "T-1", "Note", {"text": "made real progress"}, actor="r")
    snap_mod.rebuild(home, "T-1")
    assert snap_mod.load(home, "T-1").observed_seq != stale_seq

    store.write_json(disp._spawn_attempts_path(home),
                     {"T-1": {"seq": stale_seq, "count": cfg.burn_repeat_threshold}})

    rpt = burn.report(cfg, store.now_epoch())
    assert "T-1" not in rpt["no_progress_by_key"]
    assert "T-1" not in rpt["flagged"]


# --- AC2: repeated identical failure text, reproducing the measured shape ---

def test_fifteen_identical_failures_flag_burn(home, cfg):
    _seed(home, "T-1", Phase.IMPLEMENTING)
    for _ in range(15):
        event_log.append(home, "T-1", "Failed",
                         {"error": "watchdog: 5 spawns with no progress at seq 3"},
                         actor="dispatcher")
    snap_mod.rebuild(home, "T-1")

    rpt = burn.report(cfg, store.now_epoch())
    assert rpt["repeated_failure_by_key"]["T-1"] == "watchdog: 5 spawns with no progress at seq 3"
    assert "T-1" in rpt["flagged"]


def test_fifteen_different_failures_do_not_flag_burn(home, cfg):
    _seed(home, "T-1", Phase.IMPLEMENTING)
    for i in range(15):
        event_log.append(home, "T-1", "Failed", {"error": f"distinct failure #{i}"}, actor="dispatcher")
    snap_mod.rebuild(home, "T-1")

    rpt = burn.report(cfg, store.now_epoch())
    assert "T-1" not in rpt["repeated_failure_by_key"]
    assert "T-1" not in rpt["flagged"]


def test_repeated_failure_scoped_to_current_phase_visit(home, cfg):
    """A stale identical-failure streak from a phase the key has since LEFT
    (a human answering its question, a fresh fix round) must not park a key
    that is no longer stuck -- see `burn._recent_failure_texts`."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    for _ in range(5):
        event_log.append(home, "T-1", "Failed", {"error": "identical, old phase"}, actor="dispatcher")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.QA.value}, actor="r")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    rpt = burn.report(cfg, store.now_epoch())
    assert "T-1" not in rpt["repeated_failure_by_key"]


# --- AC3: a free-runner (cost 0) loop is flagged too, spend is not the only --
# --- trigger -----------------------------------------------------------------

def test_free_runner_loop_flagged_without_any_spend(home, cfg):
    _seed(home, "T-1", Phase.IMPLEMENTING)
    snap = snap_mod.load(home, "T-1")
    store.write_json(disp._spawn_attempts_path(home),
                     {"T-1": {"seq": snap.observed_seq, "count": cfg.burn_repeat_threshold + 1}})
    log_dir = home / "agent-logs" / "T-1"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "reconcile-T-1-1000.000000.opencode.jsonl").write_text(
        '{"type": "step-finish", "reason": "stop"}\n', encoding="utf-8")

    rpt = burn.report(cfg, store.now_epoch())
    assert rpt["spend_usd_by_key"].get("T-1", 0.0) == 0.0
    assert "T-1" in rpt["flagged"]


# --- AC4: real progress is never flagged, however many spawns it takes ------

def test_key_making_progress_via_distinct_failures_never_flagged(home, cfg):
    """Even a key that fails on EVERY attempt is never flagged if the text
    differs each time (real, if slow, forward motion, not a burn)."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    for i in range(20):
        event_log.append(home, "T-1", "Failed", {"error": f"attempt {i} hit a different snag"},
                         actor="dispatcher")
    snap_mod.rebuild(home, "T-1")

    rpt = burn.report(cfg, store.now_epoch())
    assert "T-1" not in rpt["flagged"]


def test_should_park_never_trips_on_a_progressing_key(home, cfg):
    _seed(home, "T-1", Phase.IMPLEMENTING)
    for i in range(cfg.burn_repeat_threshold + 5):
        event_log.append(home, "T-1", "Failed", {"error": f"different every time {i}"}, actor="r")
    snap_mod.rebuild(home, "T-1")
    assert burn.should_park(cfg, "T-1") is None


# --- AC5: a human-facing surface distinguishes "parked, waiting for you" ----
# --- from "burning" -----------------------------------------------------------

def test_status_and_needs_you_distinguish_burning_from_waiting(home, cfg):
    # A plain dead-lettered ticket -- generic failure, "waiting for you".
    _seed(home, "T-1", Phase.IMPLEMENTING)
    ops.fail(cfg, "T-1", "a real bug, not a burn", actor="reconciler", dead_letter=True)

    # A burn-parked ticket -- repeated identical failures, then parked.
    _seed(home, "T-2", Phase.IMPLEMENTING)
    ops.fail(cfg, "T-2", "burn: repeated identical failures", actor="dispatcher",
             dead_letter=True, kind="burn")

    snap1 = snap_mod.load(home, "T-1")
    snap2 = snap_mod.load(home, "T-2")
    assert snap1.phase == Phase.DEGRADED.value and snap1.burning is False
    assert snap2.phase == Phase.DEGRADED.value and snap2.burning is True

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["--home", str(home), "status"])
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    assert rc == 0
    reasons_by_key = {row[0]: row[1] for row in out["needs_you"]}
    assert reasons_by_key["T-1"] == Phase.DEGRADED.value
    assert reasons_by_key["T-2"] == "burning"

    rendered = projection.render(home)
    needs_you = rendered["NEEDS-YOU.md"]
    assert "## Dead-lettered (need a decision)" in needs_you
    assert "## Burning" in needs_you
    # Each `## ` heading starts its own section -- slice to the NEXT heading
    # (not a fixed char count) so a section's own bullet list can't bleed into
    # a neighboring section's slice regardless of which heading renders first.
    sections = {}
    for chunk in needs_you.split("\n## ")[1:]:
        title, _, body = chunk.partition("\n")
        sections[title] = body
    dead_section = sections["Dead-lettered (need a decision)"]
    burn_section = sections["Burning (parked -- no progress, repeated identical failures)"]
    assert "T-1" in dead_section
    assert "T-2" in burn_section
    assert "T-2" not in dead_section
    assert "T-1" not in burn_section

    # AC7: generated via the real write path, never hand-edited.
    written = projection.write(home)
    assert "NEEDS-YOU.md" in written
    on_disk = (home / "derived" / "NEEDS-YOU.md").read_text(encoding="utf-8")
    assert on_disk == needs_you


# --- AC6: a real sweep parks the offending key, leaves every other due key --
# --- spawning normally --------------------------------------------------------

def test_real_sweep_parks_burning_key_leaves_other_due_key_spawning(home, cfg):
    """T-1 fails with byte-identical text every round (the measured T-55/T-56
    shape, appended directly rather than through `ops.fail` -- its backoff
    branch schedules against the REAL wall clock, which would desync from
    this test's synthetic `now` and mask the very behavior under test); T-2
    makes real, distinct progress every round. `_EphemeralSessions` "dies"
    instantly, same regime as `test_spawn_attempts_reset_when_observed_seq_
    advances`, so both keys are re-considered due every sweep."""
    cfg.min_spawn_interval = 0
    cfg.burn_repeat_threshold = 3
    _seed(home, "T-1", Phase.READY)
    _seed(home, "T-2", Phase.READY)
    sessions = _EphemeralSessions()

    now = 1_000_000
    for i in range(3):
        report = disp.dispatch(cfg, sessions, now=now + i)
        assert "T-1" in report.spawned
        assert "T-2" in report.spawned
        event_log.append(home, "T-1", "Failed", {"error": "boom: identical every time"}, actor="reconciler")
        event_log.append(home, "T-2", "Note", {"text": f"progress {i}"}, actor="reconciler")
        snap_mod.rebuild(home, "T-1")
        snap_mod.rebuild(home, "T-2")

    report = disp.dispatch(cfg, sessions, now=now + 3)
    assert "T-1" not in report.spawned
    assert "T-1" in report.reaped
    assert "T-2" in report.spawned  # every OTHER due key still spawns normally

    snap1 = snap_mod.load(home, "T-1")
    assert snap1.phase == Phase.DEGRADED.value
    assert snap1.burning is True
    assert snap_mod.load(home, "T-2").phase != Phase.DEGRADED.value

    last_decision = disp.key_decisions(home, "T-1")[-1]
    assert last_decision["outcome"] == "burn_parked"


def test_burn_repeat_threshold_zero_disables_the_park(home, cfg):
    cfg.min_spawn_interval = 0
    cfg.burn_repeat_threshold = 0
    _seed(home, "T-1", Phase.READY)
    sessions = _EphemeralSessions()

    now = 1_000_000
    for i in range(6):
        report = disp.dispatch(cfg, sessions, now=now + i)
        assert "T-1" in report.spawned  # never parked -- the cap is off
        event_log.append(home, "T-1", "Failed", {"error": "boom: identical every time"}, actor="reconciler")
        snap_mod.rebuild(home, "T-1")

    assert burn.should_park(cfg, "T-1") is None
