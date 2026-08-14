"""RB-10: a reconciler that runs to completion and correctly decides nothing is
due must be distinguishable from one that crashed before appending a single
event. `_allow_spawn`'s no-progress watchdog (dispatcher.py) infers a crash
from "observed_seq did not advance" -- but a genuine no-op also appends
nothing, so both had an identical signature (the 2026-08-14 T-55 incident:
`degraded`'s passive reconciler correctly found an empty inbox, exited clean,
and was failed by the watchdog anyway, five times, over and over).

`ops.checked` (CLI: `maestro checked`) closes the gap: a `Checked{}` event the
reconciler appends immediately before `maestro release` on a genuine no-op
exit path (see `skills/maestro-reconcile-passive.md`), which advances
`observed_seq` by exactly one and makes the existing seq-comparison in
`_allow_spawn` treat the sweep as progress. No new watchdog mechanism -- one
event type plugged into the signal `_allow_spawn` already reads.
"""
from __future__ import annotations

import json

from maestro import claims, dispatcher as disp, event_log, ops, snapshot as snap_mod, store
from maestro import idempotency as step_id_mod
from maestro.cli import main as cli_main
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from test_dispatcher import _seed, _age_claim


class _HonestNoopSessions(DryRunSessions):
    """A reconciler that runs to completion, correctly decides there is
    nothing due, calls `maestro checked` (RB-10), and exits -- gone by the
    very next sweep, the same "already gone" regime `_EphemeralSessions`
    (test_dispatcher.py) models for a crash, except this one leaves the
    honest-no-op breadcrumb behind instead of nothing."""

    def __init__(self, cfg, runner="claude"):
        super().__init__()
        self._cfg = cfg
        self._runner = runner

    def list_active(self) -> set[str]:
        return set()

    def spawn(self, key, command, cwd, model=None, effort=None,
              disallowed_tools=None, allowed_tools=None, env_overlay=None,
              runner=None, runner_model=None):
        pid = super().spawn(key, command, cwd, model=model, effort=effort,
                            disallowed_tools=disallowed_tools, allowed_tools=allowed_tools,
                            env_overlay=env_overlay, runner=runner or self._runner,
                            runner_model=runner_model)
        ops.checked(self._cfg, key)
        return pid


# --- AC1: a genuine no-op does not increment the no-progress counter -------

def test_repeated_genuine_noop_produces_zero_failed_events(home, cfg):
    """N consecutive real dispatch() sweeps, each an honest no-op reconciler
    (calls `maestro checked` before exiting) -- more sweeps than
    max_spawn_attempts would tolerate for a genuine crash, and still zero
    `Failed`/`Stalled` events. This is the T-55 incident, reproduced and
    fixed: `degraded` with an empty inbox, swept repeatedly."""
    _seed(home, "T-1", Phase.DEGRADED)
    cfg.max_spawn_attempts = 3
    cfg.min_spawn_interval = 0
    sessions = _HonestNoopSessions(cfg)

    now = 1_000_000
    reports = [disp.dispatch(cfg, sessions, now=now + i) for i in range(8)]

    assert all(r.spawned == ["T-1"] for r in reports), \
        "an honest no-op must keep being spawned, never routed to failure"
    events = event_log.read(home, "T-1")
    assert all(e["type"] not in ("Failed", "Stalled") for e in events)
    snap = snap_mod.load(home, "T-1")
    assert snap.failure_count == 0
    assert snap.phase == Phase.DEGRADED.value  # never bounced to degraded-via-failure (already there)

    # The event log grew by exactly one Checked{} per sweep -- the whole cost.
    checked_events = [e for e in events if e["type"] == "Checked"]
    assert len(checked_events) == 8
    assert all(e["payload"] == {} for e in checked_events)


# --- AC2: a crash (nothing appended) still trips the watchdog, unchanged ---

def test_crash_before_appending_anything_still_fails_at_max_attempts(home, cfg):
    """Unlike the honest no-op above, a session that dies before calling
    `maestro checked` (or anything else) leaves observed_seq untouched --
    still caught, still at exactly max_spawn_attempts, unchanged from before
    this ticket. Mirrors test_dispatcher.py's
    test_spawn_attempts_fail_after_max_with_no_progress against the same
    seeded phase this file's happy-path test uses."""
    from test_dispatcher import _EphemeralSessions

    _seed(home, "T-1", Phase.DEGRADED)
    cfg.max_spawn_attempts = 3
    cfg.min_spawn_interval = 0
    sessions = _EphemeralSessions()  # dies instantly every sweep, appends nothing

    now = 1_000_000
    reports = [disp.dispatch(cfg, sessions, now=now + i) for i in range(4)]

    assert [r.spawned for r in reports[:3]] == [["T-1"]] * 3
    assert reports[3].spawned == []
    assert "T-1" in reports[3].reaped
    events = event_log.read(home, "T-1")
    assert any(e["type"] == "Failed" for e in events)
    assert all(e["type"] != "Checked" for e in events)


# --- AC3: a watchdog-killed session counts as a crash, never a completed no-op --

def test_watchdog_killed_session_never_emits_checked(home, cfg):
    """A wedged session reaped by `run_watchdog` (killed by pgid) never
    reaches `maestro checked` -- it is mid-step, not exited clean -- so it
    cannot counterfeit a completed no-op. Same shape as
    test_dispatcher.py's test_watchdog_kills_aged_claim_and_fails_ticket,
    plus the explicit absence-of-Checked assertion RB-10 adds."""
    cfg.max_session_seconds = 100
    _seed(home, "T-1", Phase.DEGRADED)
    claims.write_claim(home, "T-1", 2_000_000_001, "reconcile-T-1")
    _age_claim(home, "T-1", store.now_epoch() - 10_000)

    reaped = disp.run_watchdog(cfg, now=store.now_epoch())

    assert reaped == ["T-1"]
    events = event_log.read(home, "T-1")
    assert any(e["type"] == "Failed" for e in events)
    assert all(e["type"] != "Checked" for e in events)


# --- AC4: runner-agnostic -- the identical shape holds for claude and opencode --

def test_genuine_noop_signal_is_runner_agnostic():
    """`maestro checked` is a CLI verb the reconcile *skill script* calls --
    the same script runs unmodified whether the dispatcher attributes the
    spawn to `claude` or `opencode` (RF-2/`claims.write_claim`'s runner
    field). There is exactly one code path to the event; this proves the
    same no-progress-reset outcome holds for both attributions."""
    import tempfile
    from pathlib import Path
    from maestro.config import Config

    for runner in ("claude", "opencode"):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            for d in ("events", "inbox", "tickets", "worktrees", "derived/snapshots", "derived/cursors"):
                (home / d).mkdir(parents=True, exist_ok=True)
            cfg = Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3,
                        max_spawn_attempts=3, min_spawn_interval=0)
            _seed(home, "T-1", Phase.DEGRADED)
            sessions = _HonestNoopSessions(cfg, runner=runner)

            now = 1_000_000
            reports = [disp.dispatch(cfg, sessions, now=now + i) for i in range(6)]

            assert all(r.spawned == ["T-1"] for r in reports), runner
            events = event_log.read(home, "T-1")
            assert all(e["type"] not in ("Failed", "Stalled") for e in events), runner
            assert len([e for e in events if e["type"] == "Checked"]) == 6, runner


# --- AC5: the T-55 incident shape, end to end --------------------------------

def test_degraded_ticket_with_empty_inbox_does_not_accumulate_failed_stalled_pairs(home, cfg):
    """The measured incident (T-68's Notes, T-65's spec): a `degraded` ticket
    whose reconciler legitimately no-ops (empty inbox, nothing to route) must
    not accumulate Failed/Stalled pairs across many sweeps -- reproduced here
    via the real dispatch() loop and the honest-no-op session double above."""
    _seed(home, "T-1", Phase.DEGRADED)
    cfg.max_spawn_attempts = 5
    cfg.min_spawn_interval = 0
    sessions = _HonestNoopSessions(cfg)

    now = 1_000_000
    for i in range(20):  # far past what 5 consecutive no-progress spawns would have tolerated
        disp.dispatch(cfg, sessions, now=now + i)

    events = event_log.read(home, "T-1")
    assert not any(e["type"] in ("Failed", "Stalled") for e in events)


# --- AC6: cost of the quiet-path append is measured, not just claimed -------

def test_checked_event_payload_and_envelope_cost_is_small(home, cfg):
    """The payload is `{}` -- the whole per-event cost is the fixed envelope
    (type/actor/step_id/ts/seq), on-disk. Measured here rather than merely
    asserted in prose (ops.checked's own docstring states the resulting
    worst-case bytes/day at the spawn floor)."""
    _seed(home, "T-1", Phase.DEGRADED)

    ev = ops.checked(cfg, "T-1")
    assert ev is not None
    assert ev["payload"] == {}

    raw_line = json.dumps(ev, separators=(",", ":"))
    assert len(raw_line.encode("utf-8")) < 250, \
        "a single Checked event line grew unexpectedly large"


# --- idempotency: two callers racing on the SAME not-yet-advanced seq collapse --

def test_checked_collapses_two_racing_appends_at_the_same_seq(home, cfg):
    """The step-id dedup this shares with `requeue`/`impl-turn`/`fail`: two
    attempts that both compute their step id from the identical (key, phase,
    observed_seq) -- a lost race between two callers, or a write retried
    after its first attempt actually landed -- collapse to one event, not
    two. (A call that runs *after* a prior `checked` already landed instead
    reads the advanced seq and legitimately appends a second, distinct
    check-in -- see test_repeated_genuine_noop_produces_zero_failed_events
    for that shape, one per real sweep.)"""
    _seed(home, "T-1", Phase.DEGRADED)
    snap = snap_mod.load(home, "T-1")
    sid = step_id_mod.step_id("T-1", snap.phase, snap.observed_seq, "checked")

    ev1 = event_log.append(home, "T-1", "Checked", {}, actor="reconciler", step_id=sid)
    ev2 = event_log.append(home, "T-1", "Checked", {}, actor="reconciler", step_id=sid)

    assert ev1 is not None
    assert ev2 is None  # step-id dedup: the second, same-seq attempt is a no-op
    events = event_log.read(home, "T-1")
    assert len([e for e in events if e["type"] == "Checked"]) == 1


def test_checked_called_again_after_landing_is_a_second_real_checkin(home, cfg):
    """Not a duplicate: once the first `Checked` has landed, observed_seq has
    moved on, so a second call legitimately appends a second event -- the
    same shape a second `maestro impl-turn` call has."""
    _seed(home, "T-1", Phase.DEGRADED)
    before = snap_mod.load(home, "T-1").observed_seq

    ev1 = ops.checked(cfg, "T-1")
    ev2 = ops.checked(cfg, "T-1")

    assert ev1 is not None
    assert ev2 is not None
    events = event_log.read(home, "T-1")
    assert len([e for e in events if e["type"] == "Checked"]) == 2
    after = snap_mod.load(home, "T-1").observed_seq
    assert after == before + 2


# --- the real CLI verb ------------------------------------------------------

def test_checked_cli_verb_appends_and_advances_observed_seq(home, cfg):
    """`maestro checked <KEY>` end-to-end over the real CLI, not just the
    `ops` function it wraps -- proves the surface a reconciler skill actually
    invokes."""
    assert cli_main(["--home", str(home), "create", "RB-10 CLI smoke ticket",
                     "--key", "T-9", "--no-nudge"]) == 0
    disp.dispatch(cfg, DryRunSessions(), now=1000)  # mints the _new inbox entry
    before = snap_mod.load(home, "T-9").observed_seq

    assert cli_main(["--home", str(home), "checked", "T-9"]) == 0

    events = event_log.read(home, "T-9")
    assert any(e["type"] == "Checked" and e["payload"] == {} for e in events)
    after = snap_mod.load(home, "T-9").observed_seq
    assert after == before + 1
