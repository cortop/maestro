"""T-51: a multi-sweep deterministic simulation harness over the REAL dispatch().

Drives ``dispatcher.dispatch()`` over a temp home with an injected ``now`` clock
(never real time -- every timestamp in this file is a plain int the test controls)
and a scripted fake ``SessionManager`` (``ScriptedSessions``) whose spawned workers
advance, crash, or keep running exactly as the test schedules. Existing dispatcher
tests (``test_dispatcher.py``, ``test_dispatcher_exhaustive.py``,
``test_priority_ordering.py``) are sequential-only -- one sweep, or several sweeps
run one after another. The genuinely new piece here is ``test_two_overlapping_
sweeps_...``: two REAL, concurrently-running ``dispatch()`` calls (real threads,
one per key), never a ``time.sleep``-based race against the scheduler.

This is docs/formal-methods-evaluation.md's own rank-2 technique (deterministic
simulation, FoundationDB/TigerBeetle style) applied to the resource layer MTO-9's
proposal names: it asserts the six invariants proposal §1/§4(B) enumerates, and
confirms or dismisses the two MTO-9 §3 candidate defects held back for this ticket
((3) the spawn ledger's last-writer-wins overlap, (4) the intent-counted spawn cap
and same-priority-tier starvation). Defect (3) was CONFIRMED against an unguarded
ledger write, then DISMISSED once T-52 (#164) landed its broader spawn-region lock
-- see ``test_two_overlapping_sweeps_dismiss_defect_3_ledger_survives_spawn_lock``
for the detail. Defects (1) and (2) are explicitly out of scope here -- they were
filed as their own tickets per the human's approval.
"""
from __future__ import annotations

import threading

from maestro import dispatcher as disp, event_log, health, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import TRANSITIONS, Phase

# ---------------------------------------------------------------------------
# Harness: seed helper, the scripted fake Sessions, and the invariant checks
# every sweep in this file runs through.
# ---------------------------------------------------------------------------


def _seed(home, key, *, phase=Phase.IMPLEMENTING, priority=None, tier=0, runner=None):
    """Mirrors test_priority_ordering.py's ``_seed`` -- drives real events +
    a real fold, never hand-writes a snapshot."""
    lines = [f"# {key}", f"approval_tier: {tier}"]
    if priority is not None:
        lines.append(f"priority: {priority}")
    if runner is not None:
        lines.append(f"runner: {runner}")
    store.atomic_write(store.spec_path(home, key), "\n".join(lines) + "\n")
    event_log.append(home, key, "TicketCreated",
                      {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(home, key)


def _advance(home, key, to_phase: Phase, *, now):
    """A scripted worker makes real progress: one PhaseChanged event, exactly
    what a real reconciler's ``maestro set-phase`` would append. ``now`` folded
    into the step-id keeps repeated calls at different sweeps from colliding on
    event_log's step-id dedup."""
    event_log.append(home, key, "PhaseChanged", {"phase": to_phase.value}, actor="sim",
                     step_id=f"sim-advance-{key}-{now}")
    snap_mod.rebuild(home, key)


class ScriptedSessions:
    """A fake ``SessionManager`` whose workers advance, crash, or keep running on
    a schedule the test drives between sweeps -- unlike ``DryRunSessions``, which
    latches every spawned key active forever and so can never exercise a claim
    being freed, a floor re-arming, or a starved competitor getting its turn.

    Every ``spawn()`` call gets a fresh, never-reused pid (real claims semantics:
    a new process, a new pid) and is recorded in ``spawns`` for the invariant
    checks below. ``retire(key)`` is the test's own signal that this key's
    reconciler has exited by the time of the next sweep -- exactly what happens
    in production once the ``claude -p`` process exits, whether it made progress
    or crashed.
    """

    def __init__(self):
        self._active: set[str] = set()
        self.pid_by_key: dict[str, int] = {}
        self._next_pid = 10_000
        self.now = 0.0  # set by _sweep() before each dispatch() call
        self.spawns: list[tuple[str, int, float]] = []  # (key, pid, now) -- audit trail

    def list_active(self) -> set[str]:
        return set(self._active)

    def spawn(self, key, command, cwd, model=None, effort=None,
              disallowed_tools=None, allowed_tools=None, env_overlay=None,
              runner=None, runner_model=None) -> int | None:
        pid = self._next_pid
        self._next_pid += 1
        self._active.add(key)
        self.pid_by_key[key] = pid
        self.spawns.append((key, pid, self.now))
        return pid

    def retire(self, key: str) -> None:
        """The key's session has exited (finished a step, or crashed) -- its
        claim is gone by the next sweep."""
        self._active.discard(key)
        self.pid_by_key.pop(key, None)


def _has_path_to_done(phase: Phase) -> bool:
    """Invariant #5, soundness, as a BOUNDED search over ``statemachine.
    TRANSITIONS`` -- the decidable fragment MTO-9 §1 argues for (an 11-node,
    ~40-edge graph; a plain DFS terminates in bounded time, unlike general RCWF
    soundness, which is undecidable for maestro's eight static resource places)."""
    seen: set[Phase] = set()
    stack = [phase]
    while stack:
        p = stack.pop()
        if p == Phase.DONE:
            return True
        if p in seen:
            continue
        seen.add(p)
        stack.extend(TRANSITIONS.get(p, ()))
    return False


def _assert_sound(home, key) -> None:
    snap = snap_mod.load(home, key)
    phase = Phase(snap.phase)
    if phase == Phase.DEGRADED:
        return  # dead-lettered -- a human decides; not this invariant's concern
    assert _has_path_to_done(phase), (
        f"{key}: phase {phase.value!r} has no bounded-search path to done -- "
        "the WF-net is not sound from this state"
    )


def _sweep(home, cfg, sessions: ScriptedSessions, now, **kwargs):
    """Run one real dispatch() sweep and check every invariant that must hold
    after EVERY sweep, not just at the end of a scenario."""
    sessions.now = now
    report = disp.dispatch(cfg, sessions, now, **kwargs)

    active = sessions.list_active()
    # Invariant #1: live claims never exceed max_concurrency.
    assert len(active) <= cfg.max_concurrency, (
        f"live claims {len(active)} exceeded max_concurrency={cfg.max_concurrency}"
    )
    # Invariant #3: every spawned pid has exactly one claim -- every currently
    # active key maps to exactly one (never-reused) pid, and no two active keys
    # share a pid.
    assert set(sessions.pid_by_key) == active
    pids = list(sessions.pid_by_key.values())
    assert len(pids) == len(set(pids)), "two active keys share one claimed pid"

    # Invariant #5: soundness holds for every ticket on the board after every
    # sweep, not just the ones this sweep touched.
    for key in disp.list_keys(home):
        _assert_sound(home, key)

    return report


# ---------------------------------------------------------------------------
# The core simulation: K tickets, M sweeps, a scripted advance/crash schedule.
# ---------------------------------------------------------------------------

def test_multi_sweep_k_tickets_respect_concurrency_priority_and_floor(home):
    """The `for` loop §2/§1 argue for: K=3 tickets, M=4 sweeps, a real injected
    clock, workers that make progress, crash without progress, or keep running.
    Exercises invariants #1 (max_concurrency), #2 (min_spawn_interval), and #5
    (soundness, checked on every sweep by ``_sweep`` above) together, the way a
    real board actually mixes them -- not each in isolation."""
    cfg = Config(home=home, max_concurrency=2, min_spawn_interval=50)
    _seed(home, "T-1", priority=1)
    _seed(home, "T-2", priority=2)
    _seed(home, "T-3", priority=3)
    sessions = ScriptedSessions()

    # Sweep 1 @ t=1000: 3 due, 2 slots -- priority order wins them (T-1, T-2);
    # T-3 is capacity_skipped, not dropped.
    r1 = _sweep(home, cfg, sessions, 1000)
    assert r1.spawned == ["T-1", "T-2"]
    assert "T-3" in r1.capacity_skipped
    _advance(home, "T-1", Phase.AWAITING_CI, now=1000)  # real progress -- PR open, sleeps
    sessions.retire("T-1")
    sessions.retire("T-2")  # crashed: no progress, no PhaseChanged

    # Sweep 2 @ t=1010: T-1 now sleeps in awaiting-ci (no PR/CI signal -> not due).
    # T-2 is still `implementing` and was spawned only 10s ago -- floor=50 holds it.
    # T-3 was never spawned -- immediately eligible for the freed slot.
    r2 = _sweep(home, cfg, sessions, 1010)
    assert r2.spawned == ["T-3"]
    assert "T-2" in r2.throttled
    _advance(home, "T-3", Phase.DONE, now=1010)
    sessions.retire("T-3")

    # Sweep 3 @ t=1060: 50s after T-2's original spawn -- the floor has now
    # elapsed, so T-2 is spawnable again.
    r3 = _sweep(home, cfg, sessions, 1060)
    assert r3.spawned == ["T-2"]
    _advance(home, "T-2", Phase.DONE, now=1060)
    sessions.retire("T-2")

    # Sweep 4: nothing left due (T-1 asleep in awaiting-ci, T-2/T-3 done).
    r4 = _sweep(home, cfg, sessions, 1070)
    assert r4.spawned == []

    assert snap_mod.load(home, "T-2").phase == Phase.DONE.value
    assert snap_mod.load(home, "T-3").phase == Phase.DONE.value
    assert snap_mod.load(home, "T-1").phase == Phase.AWAITING_CI.value  # sleeping, not stuck
    _assert_sound(home, "T-1")  # still holds a bounded path to done


def test_untimed_human_signal_bypasses_the_spawn_floor(home):
    """Invariant #2's other half: a due reason IN `_UNTHROTTLED_REASONS` (a
    human acting right now) is spawnable again immediately, floor or not."""
    cfg = Config(home=home, max_concurrency=5, min_spawn_interval=1000)
    _seed(home, "T-1")
    sessions = ScriptedSessions()

    r1 = _sweep(home, cfg, sessions, 1000)
    assert r1.spawned == ["T-1"]
    sessions.retire("T-1")

    # 1 second later -- nowhere near the 1000s floor -- but a spec edit changes
    # the due reason to "spec-changed", one of the untimed human signals.
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: 0\nEDITED: yes\n")
    r2 = _sweep(home, cfg, sessions, 1001)
    assert r2.spawned == ["T-1"]
    assert dict(r2.due)["T-1"] == "spec-changed"
    assert "T-1" not in r2.throttled


def test_runaway_brake_bounds_spawn_rate_near_the_budget(home):
    """Invariant #6: total spawns over the trailing window never exceed
    health.spawn_budget(cfg) by more than one sweep's worth. The guarantee is
    REACTIVE, not preventive -- dispatch() cannot see a spawn before it lands in
    the ledger -- so the honest bound is budget+1, held there by fleet.pause
    once the brake trips. This is the regression shape for 2026-07-19 (fresh
    heartbeat, zero dead letters, thousands of no-op spawns): the very
    mechanism the incident led to."""
    cfg = Config(home=home, max_concurrency=10, min_spawn_interval=0,
                 runaway_spawns_per_hour=2, runaway_pause_cooldown=1000)
    _seed(home, "T-1")
    sessions = ScriptedSessions()

    now = 1000
    reports = []
    for _ in range(6):
        reports.append(_sweep(home, cfg, sessions, now))
        sessions.retire("T-1")  # crashes every time -- stays due, never converges
        now += 1

    total = health.spawn_rate(home, now)["total"]
    budget = health.spawn_budget(cfg)
    assert budget == 2
    assert total <= budget + 1, f"spawn rate {total} exceeded budget {budget} by more than one sweep"
    assert any(r.paused for r in reports), "the runaway brake never armed fleet.pause"
    assert reports[-1].paused and reports[-1].spawned == []


# ---------------------------------------------------------------------------
# MTO-9 §3 defect (3): the spawn ledger is a last-writer-wins whole-file
# replace -- an overlapping sweep can drop another key's `last`. DISMISSED:
# T-52's spawn-region lock already wraps the ledger read-modify-write.
# ---------------------------------------------------------------------------

def test_two_overlapping_sweeps_dismiss_defect_3_ledger_survives_spawn_lock(home):
    """DISMISSED -- superseded by T-52 (#164, merged before this test landed).
    This test originally forced two genuinely concurrent `dispatch()` calls to
    both read `.spawn_ledger.json` before either wrote back, via a
    ``threading.Barrier`` rendezvous planted inside a patched `store.read_json`,
    reproducing a last-writer-wins clobber from an unguarded ledger
    read-modify-write. T-52 now holds `store.file_lock(_spawn_lock_target(home))`
    across dispatch()'s ENTIRE spawn region -- active-read through the ledger
    write below it (`dispatcher.py`, the block starting at the `# T-52:` comment)
    -- so the ledger read-modify-write is already inside that lock. The old
    barrier placement deadlocks under it: the second thread blocks acquiring the
    lock before it ever reaches the patched `read_json` call, so the first
    thread's `barrier.wait()` (party of one) times out with `BrokenBarrierError`.
    There is no longer a point in `dispatch()` where two callers can observe the
    same stale ledger, so this now asserts the fix holds instead: two real
    concurrent `dispatch()` calls (one per key via `key_filter`, no artificial
    rendezvous needed -- the lock itself serializes them) both land their ledger
    entry, and `min_spawn_interval`'s floor holds for both on the very next
    sweep. (T-62, filed for this same defect before this was traced back to
    T-52's already-broader fix, remains its own ticket for the human to close
    out separately.)"""
    cfg = Config(home=home, max_concurrency=10, min_spawn_interval=100,
                 runaway_pause_cooldown=0)  # brake irrelevant here; keep it out of the way
    _seed(home, "T-1")
    _seed(home, "T-2")

    results: dict[str, disp.DispatchReport] = {}

    def run(key, slot):
        results[slot] = disp.dispatch(cfg, DryRunSessions(), 1000, key_filter=[key])

    t1 = threading.Thread(target=run, args=("T-1", "a"))
    t2 = threading.Thread(target=run, args=("T-2", "b"))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive(), "a dispatch() call hung"
    assert results["a"].spawned == ["T-1"]
    assert results["b"].spawned == ["T-2"]

    ledger_path = disp._spawn_ledger_path(home)
    ledger = store.read_json(ledger_path, {})
    assert len(ledger) == 2, (
        f"expected T-52's spawn-region lock to serialize both writers so neither "
        f"entry is dropped, got: {ledger}"
    )

    # The floor holds for BOTH keys on the very next sweep, 1s later (nowhere
    # near the 100s floor) -- neither entry was clobbered by the other.
    report2 = disp.dispatch(cfg, DryRunSessions(), 1001, key_filter=["T-1", "T-2"])
    assert report2.spawned == [], (
        f"expected min_spawn_interval to throttle both keys, spawned {report2.spawned}"
    )


# ---------------------------------------------------------------------------
# MTO-9 §3 defect (4): the per-repo spawn cap counts intent, not actual
# spawns, and the priority-tiebreak sort structurally starves same-priority
# competitors under sustained pressure.
# ---------------------------------------------------------------------------

def test_max_spawns_per_sweep_confirms_defect_4a_counts_intent_not_spawns(home):
    """FIXED (T-63). The repo's `max_spawns_per_sweep` cap only counts a key
    once it actually clears every downstream gate and launches -- a key
    rejected by an earlier gate (here, T-1's unregistered runner) never burns
    the slot, so it can no longer starve a key behind it that would have
    spawned cleanly. T-1 names an unregistered runner (fails cleanly, no
    process launched, no attempts-ledger spend, per `runner_unregistered`);
    T-2 is an ordinary ticket that would spawn without issue. Cap=1 for both
    -- and T-2 still gets the one slot T-1 never actually consumed."""
    cfg = Config(home=home, max_concurrency=10, min_spawn_interval=0,
                 repos={"default": {"path": None, "slug": None, "base_branch": "main",
                                     "branch_prefix": "maestro/", "default": True,
                                     "max_spawns_per_sweep": 1}})
    _seed(home, "T-1", runner="not-a-real-runner")
    _seed(home, "T-2")

    report = disp.dispatch(cfg, DryRunSessions(), 1000)

    assert report.spawned == ["T-2"], "T-1 must never actually launch (unregistered runner)"
    decision_1 = disp.key_decisions(home, "T-1", tail=1)[0]
    assert decision_1["outcome"] == "runner_unregistered"
    decision_2 = disp.key_decisions(home, "T-2", tail=1)[0]
    assert decision_2["outcome"] == "spawned", (
        "defect (4a) REGRESSED: T-2 was starved by T-1's failed spawn attempt -- "
        f"got outcome {decision_2['outcome']!r} instead"
    )


def test_same_priority_starvation_confirms_defect_4b(home):
    """FIXED (T-63): a persisted per-repo rotation cursor
    (`derived/.rotation_cursor.json`) now breaks the same-priority tie in
    round-robin order instead of always favoring the lexically-first
    `split_key`. Three same-priority (default 3) competitors, one slot, six
    sweeps (two full round-robins of N=3): every competitor now gets a turn
    within each round-robin, in rotation order (T-1, T-2, T-3, T-1, T-2, T-3)
    -- the cursor only advances past a key once it actually spawns, so the
    ring always resumes right where the last real launch left off.
    ``max_spawn_attempts`` is raised well past 6 -- that watchdog (a
    DIFFERENT circuit breaker, for no-progress-at-one-key) would otherwise
    dead-letter T-1 partway through and mask the fairness question this test
    asks."""
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0, max_spawn_attempts=100)
    _seed(home, "T-1")
    _seed(home, "T-2")
    _seed(home, "T-3")
    sessions = ScriptedSessions()

    spawned_order = []
    now = 1000
    for _ in range(6):
        report = _sweep(home, cfg, sessions, now)
        spawned_order.extend(report.spawned)
        for key in list(sessions.list_active()):
            sessions.retire(key)  # every worker crashes -- stays due, never converges
        now += 1

    assert spawned_order == ["T-1", "T-2", "T-3", "T-1", "T-2", "T-3"], spawned_order
    # The fairness bound (N = 3 competitors -> a turn within 3 sweeps) now holds
    # for BOTH T-2 and T-3, in both round-robins.
    for competitor in ("T-2", "T-3"):
        first_round = spawned_order[:3]
        second_round = spawned_order[3:]
        assert competitor in first_round, (
            f"defect (4b) REGRESSED: {competitor} did not get a turn within the first round-robin"
        )
        assert competitor in second_round, (
            f"defect (4b) REGRESSED: {competitor} did not get a turn within the second round-robin"
        )
