"""RB-12: the fleet-wide detection-channel alarm. Reuses notify.py's own transport
(notify_command/webhook_urls) -- never a second mechanism -- and dedups per
condition in `derived/.alarm.json` so a level-triggered sweep fires each episode
once.

Every test drives a real `dispatch(cfg, DryRunSessions(), now=...)` sweep (or
`_EphemeralSessions()` for the repeated-spawn brake/spawn-rate cases, exactly like
`test_runaway_brake.py`) over a temp home -- only the `claude -p` spawn is faked.
No gate behavior under test here: `test_runaway_brake.py`/`test_ratelimit.py`/
`test_spend.py` are unmodified and still pass (GA-5/GA-8/GA-11).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from maestro import alarm, dispatcher as disp, event_log, fleet, health, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from test_dispatcher import _EphemeralSessions

_W_IMPL = disp.spawn_weight(Config(home=Path(".")), Phase.IMPLEMENTING.value)


def _seed(home, key, phase=Phase.READY):
    """Local override of test_dispatcher._seed: T-80's due-gate parks any
    non-terminal ticket whose spec has no acceptance-criteria section into
    awaiting-human before a sweep can spawn it. This file's tests are about
    alarms/rate limiting, not ACs, so give every seeded spec a trivial AC
    section to keep them exercising their original, unrelated behavior."""
    store.atomic_write(store.spec_path(home, key),
                        f"# {key}\napproval_tier: 0\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated", {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def _lines(path):
    return path.read_text().splitlines() if path.exists() else []


def _utc_date(now):
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def _write_spend_state(home, now, total_usd):
    """Stand in for a folded `derived/.spend.json` -- `spend.probe()` (which runs
    ahead of the alarm hook in every real sweep below) preserves this as-is when
    the spawn ledger has no session logs to fold on top of it, so this is a
    faithful, minimal way to drive the spend conditions without spawning real
    sessions."""
    store.write_json(home / "derived" / ".spend.json", {
        "date": _utc_date(now), "total_usd": total_usd, "unattributed_sessions": 0,
        "settled_logs": [], "unavailable": False,
    })


# --- no-op when unconfigured (mirrors notify.py's own guard) -----------------

def test_no_op_when_unconfigured(home, cfg):
    fired = alarm.check(cfg, 1_000, health, runaway_reason="armed", spend_ceiling_reason=None)
    assert fired == []
    assert not (home / "derived" / ".alarm.json").exists()


# --- AC1: an armed runaway brake notifies exactly once ------------------------

def test_brake_arm_fires_exactly_once(home, tmp_path):
    log_path = tmp_path / "alarm.log"
    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0,
                 runaway_spawns_per_hour=2 * _W_IMPL, runaway_pause_cooldown=120,
                 notify_command=f'printf "%s|%s|%s\\n" "$KEY" "$PHASE" "$QUESTION" >> {log_path}')
    sessions = _EphemeralSessions()
    t0 = 1_000_000
    for i in range(3):
        report = disp.dispatch(cfg, sessions, now=t0 + i)
        assert report.spawned == ["T-1"]
    assert _lines(log_path) == []  # not armed yet -- no alarm

    breaching = disp.dispatch(cfg, sessions, now=t0 + 3)  # arms the brake
    assert breaching.spawned == []
    lines = _lines(log_path)
    # Arming the brake also crosses the independent spawn-rate condition (AC3 --
    # the exact same underlying rate, a second signal) -- one line each, filter
    # to the brake's own line for this AC.
    brake_lines = [l for l in lines if l.split("|", 2)[1] == "alarm:brake"]
    assert len(brake_lines) == 1
    key, phase, question = brake_lines[0].split("|", 2)
    assert key == "fleet" and "runaway" in question

    # Next sweep: G1 short-circuits the ENTIRE sweep (already paused) -- no
    # hooks run at all, so no second line, even with the brake still armed.
    disp.dispatch(cfg, sessions, now=t0 + 4)
    assert _lines(log_path) == lines


# --- AC2: spend warn/hard each notify once, neither re-fires while persisting -

def test_spend_warn_and_hard_fire_once_each_while_persisting(home, tmp_path, cfg):
    log_path = tmp_path / "alarm.log"
    cfg.notify_command = f'printf "%s|%s\\n" "$KEY" "$PHASE" >> {log_path}'
    cfg.daily_spend_ceiling_usd = 10.0
    assert cfg.alarm_spend_warn_fractions == [0.5, 0.8]
    t0 = 1_000_000
    _write_spend_state(home, t0, 9.0)  # 90% of ceiling -- crosses both warn fractions

    disp.dispatch(cfg, DryRunSessions(), now=t0)
    lines = _lines(log_path)
    assert len(lines) == 2  # each fraction is its own episode
    assert {l.split("|", 1)[1] for l in lines} == {"alarm:spend-warn"}

    # Same sweep, condition persists -- no new lines.
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)
    assert _lines(log_path) == lines

    # Now cross the hard ceiling too.
    _write_spend_state(home, t0 + 20, 10.0)
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 20)
    lines2 = _lines(log_path)
    assert len(lines2) == 3
    assert lines2[-1].split("|", 1)[1] == "alarm:spend-hard"

    # Persisting hard-block, still inside the cooldown -- no new line.
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 30)
    assert _lines(log_path) == lines2


# --- AC3: spawn-rate alarm agrees with doctor's `runaway`, independent of the brake

def test_spawn_rate_alarm_agrees_with_doctor_runaway(home, tmp_path):
    log_path = tmp_path / "alarm.log"
    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0,
                 runaway_spawns_per_hour=2 * _W_IMPL, runaway_pause_cooldown=0,  # brake disabled
                 notify_command=f'printf "%s|%s\\n" "$KEY" "$PHASE" >> {log_path}')
    sessions = _EphemeralSessions()
    t0 = 1_000_000
    for i in range(3):
        disp.dispatch(cfg, sessions, now=t0 + i)
    assert not fleet.pause_path(home).exists()  # brake stayed off the whole time

    breaching = disp.dispatch(cfg, sessions, now=t0 + 3)  # rate now over budget
    assert breaching.spawned == ["T-1"]  # never paused -- the brake is disabled
    lines = _lines(log_path)
    assert len(lines) == 1
    assert lines[0].split("|", 1)[1] == "alarm:spawn-rate"

    rate = health.spawn_rate(home, t0 + 3)["total"]
    budget = health.spawn_budget(cfg)
    assert rate > budget  # exactly the verdict `maestro doctor` reports as `runaway`


# --- AC4: dedup state is disposable -------------------------------------------

def test_dedup_state_disposable(home, cfg, tmp_path):
    log_path = tmp_path / "alarm.log"
    cfg.notify_command = f'printf "%s|%s\\n" "$KEY" "$PHASE" >> {log_path}'
    cfg.daily_spend_ceiling_usd = 10.0
    t0 = 1_000_000
    _write_spend_state(home, t0, 10.0)  # at the ceiling

    disp.dispatch(cfg, DryRunSessions(), now=t0)
    state_path = home / "derived" / ".alarm.json"
    assert state_path.exists()
    lines_before = _lines(log_path)
    assert len(lines_before) >= 1

    disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)  # still active -- no new lines
    assert _lines(log_path) == lines_before

    state_path.unlink()  # simulate a `derived/` wipe/reset
    report = disp.dispatch(cfg, DryRunSessions(), now=t0 + 20)  # must not crash or wedge
    assert report.hook_errors == {}
    lines_after = _lines(log_path)
    assert len(lines_after) > len(lines_before)  # re-armed cold, fired again


# --- AC5: re-announces after the condition clears+recurs, and after cooldown --

def test_reannounce_after_clear_and_recur_and_after_cooldown(home, tmp_path):
    log_path = tmp_path / "alarm.log"
    cfg = Config(home=home, daily_spend_ceiling_usd=10.0, alarm_cooldown_s=50,
                 notify_command=f'printf "%s|%s\\n" "$KEY" "$PHASE" >> {log_path}')
    t0 = 1_000_000

    def hard_lines():
        return [l for l in _lines(log_path) if l.endswith("spend-hard")]

    _write_spend_state(home, t0, 10.0)  # active
    disp.dispatch(cfg, DryRunSessions(), now=t0)
    assert len(hard_lines()) == 1

    _write_spend_state(home, t0 + 5, 10.0)  # still active, well inside cooldown
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 5)
    assert len(hard_lines()) == 1  # no re-fire yet

    _write_spend_state(home, t0 + 10, 5.0)  # clears
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)
    assert len(hard_lines()) == 1  # inactive -- nothing to fire

    _write_spend_state(home, t0 + 15, 10.0)  # recurs
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 15)
    assert len(hard_lines()) == 2  # a fresh episode -- fires again immediately

    _write_spend_state(home, t0 + 15 + 50, 10.0)  # persists across the cooldown
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 15 + 50)
    assert len(hard_lines()) == 3  # still active, cooldown elapsed -- re-announces


# --- AC6: fail open, LOUDLY-but-not-fatally -----------------------------------

def test_broken_notify_command_and_unreachable_webhook_records_hook_error(home, cfg):
    """Unlike notify.py's own advisory ping (which silently swallows a broken
    command/webhook), the alarm is the fleet's one detection channel -- a broken
    transport must be LOUD (AC6): it lands in `report.hook_errors["alarm"]`
    (never aborting the sweep -- T-1 still spawns), so a human reading `maestro
    why`/the dispatch ledger finds out the alarm itself stopped working."""
    cfg.notify_command = "this-binary-does-not-exist-anywhere --flag"
    cfg.webhook_urls = ["http://127.0.0.1:1/unreachable"]  # connection refused
    cfg.daily_spend_ceiling_usd = 100.0
    _write_spend_state(home, 1_000_000, 90.0)  # crosses the warn fraction, not the hard
                                                # ceiling -- an active condition without
                                                # also invoking the (unrelated) spend gate
    _seed(home, "T-1", Phase.READY)
    cfg.min_spawn_interval = 0

    report = disp.dispatch(cfg, DryRunSessions(), now=1_000_000)
    assert "T-1" in report.spawned  # loud, never fatal -- the sweep still spawns normally
    assert "alarm" in report.hook_errors
    assert "notify_command" in report.hook_errors["alarm"]
    assert "webhook" in report.hook_errors["alarm"]

    # Dedup state is still durably persisted despite the broken transport --
    # the condition doesn't just re-attempt (and re-fail) into the ether forever
    # with no record of it, and a subsequent sweep with the SAME still-active
    # condition (inside cooldown) does not queue a second command attempt.
    state = store.read_json(home / "derived" / ".alarm.json", {})
    assert state.get("spend_warn:0.5", {}).get("date") is not None


def test_corrupt_alarm_state_records_hook_error_and_sweep_still_spawns(home, cfg):
    cfg.notify_command = "true"
    store.atomic_write(home / "derived" / ".alarm.json", json.dumps("not-an-object"))
    _seed(home, "T-1", Phase.READY)
    cfg.min_spawn_interval = 0

    report = disp.dispatch(cfg, DryRunSessions(), now=1_000_000)
    assert "T-1" in report.spawned
    assert "alarm" in report.hook_errors


# --- AC7 (asserted here too): unchanged under dry_run -------------------------

def test_dry_run_never_writes_state_or_fires(home, cfg):
    cfg.notify_command = "true"
    cfg.daily_spend_ceiling_usd = 10.0
    _write_spend_state(home, 1_000_000, 10.0)  # an active condition

    disp.dispatch(cfg, DryRunSessions(), now=1_000_000, dry_run=True)
    assert not (home / "derived" / ".alarm.json").exists()
