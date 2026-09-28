"""T-136: exempt phase hand-offs from the 300s per-key spawn floor.

A key whose folded phase differs from the phase it was last spawned in has
already made progress -- the floor (GA-8's runaway guard, `_gate_due` in
dispatcher.py) should not hold it. A same-phase respawn (no progress) stays
throttled exactly as before, regardless of which actor made the last change
or whether the ledger entry predates this exemption.
"""
from __future__ import annotations

import io
import json
import sys
import time

from maestro import cli, dispatcher as disp, ops, snapshot as snap_mod, store
from maestro.statemachine import Phase

from conftest import seed_phase
from test_dispatcher import _EphemeralSessions


def _set_phase(home, key, phase, *, actor="reconciler", reason=""):
    args = ["--home", str(home), "set-phase", key, phase, "--reason", reason, "--actor", actor]
    rc = cli.main(args)
    assert rc == 0


def test_ready_to_implementing_handoff_bypasses_floor(home, cfg):
    seed_phase(home, "T-1", Phase.READY)
    cfg.min_spawn_interval = 300
    sessions = _EphemeralSessions()

    t0 = 1_000_000
    report = disp.dispatch(cfg, sessions, now=t0)
    assert report.spawned == ["T-1"]
    assert sessions.spawned[-1][1].startswith("/maestro-reconcile-ready")

    _set_phase(home, "T-1", "implementing", reason="worktree ready")

    report2 = disp.dispatch(cfg, sessions, now=t0 + 60)
    assert report2.spawned == ["T-1"] and report2.throttled == []
    assert sessions.spawned[-1][1].startswith("/maestro-reconcile-implementing")


def test_awaiting_human_to_ready_handoff_bypasses_floor(home, cfg):
    seed_phase(home, "T-1", Phase.AWAITING_HUMAN)  # no question -> due "stranded"
    cfg.min_spawn_interval = 300
    sessions = _EphemeralSessions()

    t0 = 1_000_000
    report = disp.dispatch(cfg, sessions, now=t0)
    assert report.spawned == ["T-1"]

    _set_phase(home, "T-1", "ready", reason="approved")

    report2 = disp.dispatch(cfg, sessions, now=t0 + 60)
    assert report2.spawned == ["T-1"] and report2.throttled == []
    assert sessions.spawned[-1][1].startswith("/maestro-reconcile-ready")


def test_dispatcher_actor_handoff_bypasses_floor(home, cfg):
    """The exemption is actor-agnostic: a phase change made by the dispatcher
    itself (e.g. a CI bounce or an in-sweep route) bypasses the floor exactly
    like one a reconciler made -- the snapshot never records who acted."""
    seed_phase(home, "T-1", Phase.READY)
    cfg.min_spawn_interval = 300
    sessions = _EphemeralSessions()

    t0 = 1_000_000
    report = disp.dispatch(cfg, sessions, now=t0)
    assert report.spawned == ["T-1"]

    _set_phase(home, "T-1", "implementing", actor="dispatcher", reason="route")

    report2 = disp.dispatch(cfg, sessions, now=t0 + 60)
    assert report2.spawned == ["T-1"] and report2.throttled == []


def test_dry_run_would_spawn_after_handoff(home):
    """`maestro dispatch --dry-run` reads the real wall clock, so seed the
    ledger's `last` as now-minus-60 rather than driving it through `dispatch`
    with a synthetic `now`."""
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    (home / "config.toml").write_text("[maestro]\nmax_concurrency = 3\nmin_spawn_interval = 300\n")
    store.write_json(disp._spawn_ledger_path(home),
                     {"T-1": {"last": time.time() - 60, "recent": [], "phase": "ready"}})

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        assert cli.main(["--home", str(home), "dispatch", "--dry-run"]) == 0
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    assert out["would_spawn"] == ["T-1"] and out["throttled"] == []


def test_qa_handoff_from_implementing_bypasses_floor(home, cfg):
    """AC4: the implementing skill's leftover `--requeue 300` is gone -- the
    exemption alone is what lets a same-sweep qa hand-off spawn immediately."""
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    cfg.min_spawn_interval = 300
    sessions = _EphemeralSessions()

    t0 = 1_000_000
    report = disp.dispatch(cfg, sessions, now=t0)
    assert report.spawned == ["T-1"]

    _set_phase(home, "T-1", "qa")

    report2 = disp.dispatch(cfg, sessions, now=t0 + 60)
    assert report2.spawned == ["T-1"] and report2.throttled == []
    assert sessions.spawned[-1][1].startswith("/maestro-reconcile-qa")


def test_no_progress_respawn_stays_throttled_in_same_phase(home, cfg):
    """No hand-off at all (the ticket never left `implementing`): every sweep
    inside the floor reports the key throttled, exactly as before this ticket."""
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    cfg.min_spawn_interval = 300
    sessions = _EphemeralSessions()

    t0 = 1_000_000
    assert disp.dispatch(cfg, sessions, now=t0).spawned == ["T-1"]
    for offset in (1, 100, 250, 299):
        r = disp.dispatch(cfg, sessions, now=t0 + offset)
        assert r.spawned == [] and r.throttled == ["T-1"]
    assert disp.dispatch(cfg, sessions, now=t0 + 300).spawned == ["T-1"]


def test_backoff_session_not_due_then_throttled_if_timer_expires_in_floor(home, cfg):
    """A session that only runs `maestro fail` (below max_failures) makes no
    phase change -- it is not due at all while its own backoff timer runs, and
    throttled by the floor (not spawned) if that timer expires before the
    floor itself does. It spawns again only once both have elapsed."""
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    cfg.min_spawn_interval = 300
    cfg.backoff_base = 10
    cfg.max_failures = 5
    sessions = _EphemeralSessions()

    t0 = time.time()
    assert disp.dispatch(cfg, sessions, now=t0).spawned == ["T-1"]

    outcome = ops.fail(cfg, "T-1", "boom")
    assert outcome.startswith("backoff:")
    base = snap_mod.load(home, "T-1").next_requeue_at
    assert base - t0 < cfg.min_spawn_interval  # this test needs the backoff to
    # expire well before the floor does, or the two checks below coincide

    # Backoff timer still running: not due at all, regardless of the floor.
    r1 = disp.dispatch(cfg, sessions, now=base - 1)
    assert r1.spawned == [] and r1.throttled == []

    # Timer expired, but still inside the floor and no phase change happened:
    # throttled, not spawned.
    r2 = disp.dispatch(cfg, sessions, now=base + 1)
    assert r2.spawned == [] and r2.throttled == ["T-1"]

    # Both the backoff and the floor have now elapsed.
    r3 = disp.dispatch(cfg, sessions, now=t0 + cfg.min_spawn_interval)
    assert r3.spawned == ["T-1"]


def test_ledger_entry_missing_phase_field_still_throttles_after_handoff(home, cfg):
    """AC5: a ledger entry written before this change (a dict with no `phase`
    key) falls back to today's throttle even though the ticket DID change
    phase -- there is nothing recorded to compare against."""
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    cfg.min_spawn_interval = 300
    store.write_json(disp._spawn_ledger_path(home), {"T-1": {"last": 1_000_000.0, "recent": []}})

    _set_phase(home, "T-1", "qa")

    sessions = _EphemeralSessions()
    report = disp.dispatch(cfg, sessions, now=1_000_060)
    assert report.spawned == [] and report.throttled == ["T-1"]


def test_legacy_bare_number_ledger_still_throttles_after_handoff(home, cfg):
    """AC5's other legacy shape: a bare float (pre-GA-14 ledger), not a dict at
    all. Falls back to today's throttle across a real phase change too."""
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    cfg.min_spawn_interval = 300
    store.write_json(disp._spawn_ledger_path(home), {"T-1": 1_000_000.0})

    _set_phase(home, "T-1", "qa")

    sessions = _EphemeralSessions()
    report = disp.dispatch(cfg, sessions, now=1_000_060)
    assert report.spawned == [] and report.throttled == ["T-1"]
