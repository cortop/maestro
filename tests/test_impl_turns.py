"""max_impl_turns enforcement (AD-2): the ImplTurnRecorded verb, its fold into
snapshot.impl_turns, and crossing the ceiling parking the ticket instead of the
agent having to honour a sentence of instructions.
"""
from maestro import cli, event_log
from maestro import events as E
from maestro import ops
from maestro import dispatcher as disp
from maestro import snapshot as snap_mod
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import seed_ticket


# ---------------------------------------------------------------------------
# AC1: a `maestro` verb appends ImplTurnRecorded{turn, role} and it folds into
# snapshot.impl_turns.
# ---------------------------------------------------------------------------

def test_impl_turn_verb_appends_event_and_folds_snapshot(home, cfg):
    seed_ticket(home, "T-1", "do the thing", phase="implementing")

    rc = cli.main(["--home", str(home), "impl-turn", "T-1", "--role", "implementer"])
    assert rc == 0

    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_TURN]
    assert len(evs) == 1
    assert evs[0]["payload"] == {"turn": 1, "role": "implementer"}

    snap = snap_mod.load(home, "T-1")
    assert snap.impl_turns == 1


def test_impl_turn_verb_increments_across_calls(home, cfg):
    seed_ticket(home, "T-1", "do the thing", phase="implementing")
    for _ in range(3):
        ops.record_impl_turn(cfg, "T-1")

    snap = snap_mod.load(home, "T-1")
    assert snap.impl_turns == 3
    turns = [e["payload"]["turn"] for e in event_log.read(home, "T-1")
             if e["type"] == E.IMPL_TURN]
    assert turns == [1, 2, 3]


def test_impl_turn_is_idempotent_under_replay(home, cfg):
    """Same (phase, observed_seq) -> same step id -> re-appending is a no-op,
    exactly like every other reconciler verb's crash-and-respawn safety."""
    seed_ticket(home, "T-1", "do the thing", phase="implementing")
    snap = snap_mod.load(home, "T-1")
    turn = snap.impl_turns + 1
    from maestro.idempotency import step_id
    sid = step_id("T-1", snap.phase, snap.observed_seq, f"implturn:{turn}")
    event_log.append(home, "T-1", E.IMPL_TURN, {"turn": turn, "role": "implementer"},
                     actor="reconciler", step_id=sid)
    event_log.append(home, "T-1", E.IMPL_TURN, {"turn": turn, "role": "implementer"},
                     actor="reconciler", step_id=sid)  # same step id -> collapsed
    snap_mod.rebuild(home, "T-1")
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_TURN]
    assert len(evs) == 1


# ---------------------------------------------------------------------------
# AC2: crossing cfg.max_impl_turns parks the ticket (fail / needs-human)
# instead of continuing, and the reason names the ceiling.
# ---------------------------------------------------------------------------

def test_crossing_ceiling_parks_via_fail_with_reason_naming_ceiling(home, cfg):
    seed_ticket(home, "T-1", "do the thing", phase="implementing")
    cfg.max_impl_turns = 2

    r1 = ops.record_impl_turn(cfg, "T-1")
    assert r1 == {"turn": 1, "parked": False}
    snap = snap_mod.load(home, "T-1")
    assert snap.failure_count == 0
    assert snap.next_requeue_at is None

    r2 = ops.record_impl_turn(cfg, "T-1")
    assert r2 == {"turn": 2, "parked": True}
    snap = snap_mod.load(home, "T-1")
    assert snap.failure_count == 1
    assert "max_impl_turns" in snap.last_error
    assert "2" in snap.last_error  # names the ceiling
    # parked -> the existing fail() backoff/dead-letter machinery, same as
    # max_spawn_attempts's watchdog
    assert snap.next_requeue_at is not None or snap.phase == Phase.DEGRADED.value


def test_under_ceiling_does_not_park(home, cfg):
    seed_ticket(home, "T-1", "do the thing", phase="implementing")
    cfg.max_impl_turns = 5
    for _ in range(4):
        result = ops.record_impl_turn(cfg, "T-1")
        assert result["parked"] is False
    snap = snap_mod.load(home, "T-1")
    assert snap.failure_count == 0


def test_max_impl_turns_zero_disables_enforcement(home, cfg):
    seed_ticket(home, "T-1", "do the thing", phase="implementing")
    cfg.max_impl_turns = 0
    for _ in range(10):
        result = ops.record_impl_turn(cfg, "T-1")
        assert result["parked"] is False
    snap = snap_mod.load(home, "T-1")
    assert snap.failure_count == 0
    assert snap.impl_turns == 10


# ---------------------------------------------------------------------------
# AC3: a real-CLI test over a temp home asserts a ticket at the ceiling stops
# advancing across a real dispatcher sweep.
# ---------------------------------------------------------------------------

def test_ticket_at_ceiling_stops_advancing_across_real_dispatcher_sweep(home, cfg):
    seed_ticket(home, "T-1", "do the thing", phase="implementing")
    # Write the ceiling to config.toml so the *separate* `cli.main()` process
    # below (which reloads config from disk, like a real reconciler would)
    # sees the same ceiling as the in-process `cfg` used to drive dispatch().
    (home / "config.toml").write_text(
        "[maestro]\nmax_impl_turns = 3\nmin_spawn_interval = 0\n")
    cfg.max_impl_turns = 3
    cfg.min_spawn_interval = 0

    # A real dispatcher sweep would be due (active phase, no PR yet).
    sessions = DryRunSessions()
    r0 = disp.dispatch(cfg, sessions, now=1_000_000)
    assert r0.spawned == ["T-1"]

    # Drive turns via the real CLI verb, exactly as a churning implementing
    # session would, until it crosses the ceiling.
    for _ in range(3):
        rc = cli.main(["--home", str(home), "impl-turn", "T-1"])
        assert rc == 0

    snap = snap_mod.load(home, "T-1")
    assert snap.impl_turns == 3
    assert "max_impl_turns" in (snap.last_error or "")

    # The next real sweep must NOT keep spawning it as if nothing happened --
    # it is parked behind the backoff timer (or dead-lettered), same as any
    # other `fail`-parked ticket.
    r1 = disp.dispatch(cfg, sessions, now=1_000_010)
    assert r1.spawned == []
    assert snap.phase == Phase.DEGRADED.value or snap_mod.load(home, "T-1").next_requeue_at is not None
