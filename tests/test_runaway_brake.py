"""GA-5: the auto-brake beside dispatch()'s G4 rate-limit gate. Arms
`fleet.pause` the moment a sweep observes exactly the signal `maestro doctor`'s
`runaway` field reports -- `health.spawn_rate(home, now)["total"] >
health.spawn_budget(cfg)` -- on a bounded, self-healing cooldown, with the
observed numbers written into the pause reason.

Every test drives real dispatch() sweeps (or the real `maestro` CLI) over a
temp home; only the `claude -p` spawn is faked (`_EphemeralSessions`, the
2026-07-19 regime: a worker gone by the very next sweep).
"""
import io
import json
import sys
from pathlib import Path

from maestro import cli, dispatcher as disp, fleet, inbox, projection, store
from maestro.config import Config
from maestro.statemachine import Phase

from test_dispatcher import _EphemeralSessions, _seed

# GA-14: `health.spawn_rate` now records one `implementing`-phase spawn as
# this many agent-equivalents (1 + max_impl_turns * 1, qa_standards_axis
# defaults off), not a bare count of 1 -- every `runaway_spawns_per_hour`
# override below that arms the brake off a fleet of `implementing` tickets is
# scaled by the same factor, so the "N real spawns before the breach" shape
# each test exercises is unchanged, just in the new unit.
_W_IMPL = disp.spawn_weight(Config(home=Path(".")), Phase.IMPLEMENTING.value)


def _sweep_cli(home, *args):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(home), *args])
    finally:
        sys.stdout = old
    return code, json.loads(buf.getvalue())


# --- direct health.spawn_rate/spawn_budget only, never the full health.report() -

def test_dispatch_never_calls_health_report(home, monkeypatch):
    """AC: dispatch() computes the observed rate from health.spawn_rate/
    spawn_budget directly -- never health.report() (which runs the full check
    registry: an extra repo preflight, a launchctl shell-out for
    check_launchctl, etc.) on every sweep."""
    from maestro import health as health_mod

    def _boom(*a, **kw):
        raise AssertionError("dispatch() must not call health.report() per sweep")

    monkeypatch.setattr(health_mod, "report", _boom)
    monkeypatch.setattr(health_mod, "check_launchctl", _boom)
    monkeypatch.setattr(health_mod, "run_checks", _boom)

    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0,
                runaway_spawns_per_hour=2 * _W_IMPL, runaway_pause_cooldown=50)
    sessions = _EphemeralSessions()
    t0 = 1_000_000
    for i in range(5):  # crosses the budget partway through -- arm path too
        disp.dispatch(cfg, sessions, now=t0 + i)


# --- arm: bounded until, reason names both numbers, breaching sweep is inert -

def test_brake_arms_pause_with_bounded_until_and_reason(home):
    _seed(home, "T-1", Phase.IMPLEMENTING)  # active phase -- due every sweep
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0,
                runaway_spawns_per_hour=2 * _W_IMPL, runaway_pause_cooldown=120)
    sessions = _EphemeralSessions()
    t0 = 1_000_000

    # Two real spawns land at/under budget==2*_W_IMPL; a third observes
    # rate==2*_W_IMPL (not yet >) and also spawns, bringing the recorded
    # total to 3*_W_IMPL.
    for i in range(3):
        report = disp.dispatch(cfg, sessions, now=t0 + i)
        assert report.spawned == ["T-1"]
    assert not fleet.pause_path(home).exists()

    ledger_before = disp._spawn_ledger_path(home).read_bytes()
    breaching = disp.dispatch(cfg, sessions, now=t0 + 3)  # rate > budget

    assert breaching.spawned == []
    assert disp._spawn_ledger_path(home).read_bytes() == ledger_before

    state = store.read_json(fleet.pause_path(home), None)
    assert state is not None
    assert state["until"] == t0 + 3 + cfg.runaway_pause_cooldown
    assert f"{3 * _W_IMPL}" in state["reason"] and f"{2 * _W_IMPL}" in state["reason"]


# --- the full arm -> short-circuit -> self-heal sequence, one temp home -----

def test_arm_short_circuits_next_sweep_then_self_heals(home):
    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0,
                runaway_spawns_per_hour=2 * _W_IMPL, runaway_pause_cooldown=50)
    sessions = _EphemeralSessions()
    t0 = 1_000_000
    for i in range(3):
        disp.dispatch(cfg, sessions, now=t0 + i)
    breaching = disp.dispatch(cfg, sessions, now=t0 + 3)
    assert breaching.spawned == []
    until = store.read_json(fleet.pause_path(home), {})["until"]

    # Queued only now (after the breach), so a mint from one of the earlier,
    # still-normal sweeps above can't drain it first.
    inbox.append_new(home, "queued while auto-paused", key="T-9")

    # Next sweep: G1 short-circuits entirely -- no mint, no due, no spawn.
    next_sweep = disp.dispatch(cfg, sessions, now=t0 + 4)
    assert next_sweep.paused is True
    assert next_sweep.minted == [] and next_sweep.due == [] and next_sweep.spawned == []
    assert inbox.pending_new(home) != []  # the queued create-request untouched

    # Self-heal: once `until` has passed, no human action needed.
    healed = disp.dispatch(cfg, sessions, now=until + 1)
    assert healed.paused is False
    assert healed.spawned == ["T-1"]
    assert not fleet.pause_path(home).exists()


# --- no resume wedge ----------------------------------------------------------

def test_resume_does_not_immediately_rewedge_without_truncating_ledger(home):
    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0,
                runaway_spawns_per_hour=2 * _W_IMPL, runaway_pause_cooldown=1800)
    sessions = _EphemeralSessions()
    t0 = 1_000_000
    for i in range(3):
        disp.dispatch(cfg, sessions, now=t0 + i)
    disp.dispatch(cfg, sessions, now=t0 + 3)  # arms a long (1800s) pause
    assert fleet.pause_path(home).exists()
    ledger_before_resume = disp._spawn_ledger_path(home).read_bytes()

    assert cli.main(["--home", str(home), "fleet", "resume"]) == 0
    assert not fleet.pause_path(home).exists()
    # The resume itself never touches the ledger -- only a real spawn does.
    assert disp._spawn_ledger_path(home).read_bytes() == ledger_before_resume

    # A real sweep immediately after, still inside the trailing window (the
    # ledger's `recent` history is unchanged and still says rate > budget) --
    # must NOT immediately re-arm the pause.
    resumed_sweep = disp.dispatch(cfg, sessions, now=t0 + 4)
    assert not fleet.pause_path(home).exists()
    assert resumed_sweep.spawned == ["T-1"]  # a normal sweep, not a short-circuit


def test_resume_suppression_expires_and_brake_resumes_working(home):
    """The post-resume grace period is bounded, not a permanent disable --
    once it elapses the brake can trip again on a still-hot rate. The grace
    is anchored to the prior armed pause's own `until` (t0+13) + cooldown
    (10) == t0+23, independent of exactly when the human resumed."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0,
                runaway_spawns_per_hour=2 * _W_IMPL, runaway_pause_cooldown=10)
    sessions = _EphemeralSessions()
    t0 = 1_000_000
    for i in range(3):
        disp.dispatch(cfg, sessions, now=t0 + i)
    disp.dispatch(cfg, sessions, now=t0 + 3)  # arm: until == t0+3+10 == t0+13
    assert cli.main(["--home", str(home), "fleet", "resume"]) == 0

    # Still inside the grace window (< t0+23): no re-arm, spawns normally.
    still_suppressed = disp.dispatch(cfg, sessions, now=t0 + 4)
    assert not fleet.pause_path(home).exists()
    assert still_suppressed.spawned == ["T-1"]

    # Past t0+23, with the rate still elevated (four recorded spawns now, all
    # still inside the 3600s window): trips again.
    breaching_again = disp.dispatch(cfg, sessions, now=t0 + 25)
    assert fleet.pause_path(home).exists()
    assert breaching_again.spawned == []


# --- one number, one verdict: human-signal spawns count; matches doctor -----

def test_human_signal_spawns_count_toward_brake_same_total_as_doctor(home):
    """A burst of _UNTHROTTLED_REASONS ("inbox") spawns bypasses the per-key
    floor by design, but NOT the brake -- it arms just like any other spawn,
    and the sweep that arms it agrees with `maestro doctor`'s own verdict on
    the same observed total."""
    (home / "config.toml").write_text("[maestro]\nrunaway_spawns_per_hour = 2\n")
    _seed(home, "T-1", Phase.READY)  # sleeping phase -- due only via the inbox signal
    inbox.append_command(home, "T-1", "ans", {"qid": "q1", "text": "go"})
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=300,
                runaway_spawns_per_hour=2, runaway_pause_cooldown=120)
    sessions = _EphemeralSessions()
    t0 = store.now_epoch()  # doctor reads the ledger against the real wall clock

    for i in range(3):
        report = disp.dispatch(cfg, sessions, now=t0 + i)
        assert report.spawned == ["T-1"] and report.throttled == []  # floor bypassed

    breaching = disp.dispatch(cfg, sessions, now=t0 + 3)
    assert breaching.spawned == []
    state = store.read_json(fleet.pause_path(home), {})
    assert "3" in state["reason"]

    code, out = _sweep_cli(home, "doctor")
    assert out["spawns_last_hour"]["total"] == 3
    assert out["spawn_budget_per_hour"] == 2
    assert out["runaway"] is True
    assert code == 1


# --- config: one threshold, two consumers; a separate cooldown knob ---------

def test_runaway_spawns_per_hour_zero_disables_both(home):
    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0,
                runaway_spawns_per_hour=0, runaway_pause_cooldown=120)
    sessions = _EphemeralSessions()
    t0 = store.now_epoch()
    for i in range(20):
        disp.dispatch(cfg, sessions, now=t0 + i)
    assert not fleet.pause_path(home).exists()

    (home / "config.toml").write_text("[maestro]\nrunaway_spawns_per_hour = 0\n")
    code, out = _sweep_cli(home, "doctor")
    assert out["runaway"] is False and out["spawn_budget_per_hour"] == 0
    assert code == 0


def test_cooldown_zero_disables_auto_pause_but_not_doctor(home):
    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0,
                runaway_spawns_per_hour=2, runaway_pause_cooldown=0)
    sessions = _EphemeralSessions()
    t0 = store.now_epoch()
    for i in range(10):
        disp.dispatch(cfg, sessions, now=t0 + i)
    assert not fleet.pause_path(home).exists()  # auto-brake off

    (home / "config.toml").write_text("[maestro]\nrunaway_spawns_per_hour = 2\n")
    code, out = _sweep_cli(home, "doctor")
    assert out["runaway"] is True  # doctor's advisory is untouched
    assert code == 1


def test_cooldown_configurable_via_config_toml(home):
    (home / "config.toml").write_text(
        "[maestro]\nmax_concurrency = 1\nmin_spawn_interval = 0\n"
        "runaway_spawns_per_hour = 2\nrunaway_pause_cooldown = 55\n")
    from maestro import config as config_mod
    cfg = config_mod.load(str(home))
    assert cfg.runaway_pause_cooldown == 55


# --- legible on the real surfaces without further work -----------------------

def test_legible_on_fleet_status_dispatch_json_workstate_and_doctor(home):
    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0,
                runaway_spawns_per_hour=2 * _W_IMPL, runaway_pause_cooldown=120)
    sessions = _EphemeralSessions()
    t0 = store.now_epoch()
    for i in range(3):
        disp.dispatch(cfg, sessions, now=t0 + i)
    disp.dispatch(cfg, sessions, now=t0 + 3)  # arms the pause
    assert fleet.pause_path(home).exists()

    code, out = _sweep_cli(home, "fleet", "status")
    assert code == 0
    assert out["paused"] is True
    assert "runaway" in (out["pause_reason"] or "")
    assert out["pause_until"] is not None

    (home / "config.toml").write_text(
        "[maestro]\nmax_concurrency = 1\nmin_spawn_interval = 0\n"
        "runaway_spawns_per_hour = 2\nrunaway_pause_cooldown = 120\n")
    # A real (non-dry-run) CLI dispatch: safe here because G1 short-circuits
    # BEFORE anything touches `sessions` (no claude -p spawn is ever reached).
    code, out = _sweep_cli(home, "dispatch")
    assert code == 0
    assert out["paused"] is True
    assert out["spawned"] == [] and out["due"] == []

    projection.write(home)
    workstate = (home / "derived" / "WORKSTATE.md").read_text()
    assert "Fleet paused" in workstate and "runaway" in workstate

    code, out = _sweep_cli(home, "doctor")
    assert code == 1
    assert out["runaway"] is True
    assert "spawn_budget_per_hour" in out and "spawns_last_hour" in out
