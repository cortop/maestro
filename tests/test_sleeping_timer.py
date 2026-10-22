"""T-193: an elapsed requeue timer no longer wakes the dispatcher-owned sleeping phases."""
from __future__ import annotations

import pytest
from conftest import run_doctor, seed_phase

from maestro import dispatcher as disp
from maestro import inbox, ops
from maestro import snapshot as snap_mod
from maestro.sessions import DryRunSessions
from maestro.statemachine import TERMINAL_PHASES, Phase

OWNED = [Phase.AWAITING_CI, Phase.IN_REVIEW, Phase.VERIFYING]


def _due(home, key, now, **kw):
    snap = snap_mod.load(home, key)
    kw.setdefault("current_spec_hash", snap.spec_hash)
    return disp.is_due(home, key, snap, inbox_pending=kw.pop("inbox_pending", False), now=now, **kw)


@pytest.mark.parametrize("phase", OWNED)
def test_elapsed_timer_does_not_wake_sleeping_phases(home, cfg, phase):
    seed_phase(home, "T-1", phase)
    ops.requeue(cfg, "T-1", 100)
    base = snap_mod.load(home, "T-1").next_requeue_at
    sessions = DryRunSessions()
    assert disp.dispatch(cfg, sessions, now=base - 50).spawned == []
    for dt in (1, 500, 5000):
        assert disp.dispatch(cfg, sessions, now=base + dt).spawned == []
    assert not _due(home, "T-1", base + 1).due


@pytest.mark.parametrize("phase", [p for p in Phase if p not in TERMINAL_PHASES])
def test_timer_rules_unchanged_elsewhere(home, cfg, phase):
    seed_phase(home, "T-1", phase)
    if phase == Phase.AWAITING_HUMAN:
        ops.ask(cfg, "T-1", "ok?", qid="q1")
    ops.requeue(cfg, "T-1", 900)
    base = snap_mod.load(home, "T-1").next_requeue_at
    held = _due(home, "T-1", base - 1)
    assert not held.due and held.reason == "backoff"
    woke = _due(home, "T-1", base + 1)
    if phase in OWNED:
        assert not woke.due
    else:
        assert woke.due and woke.reason == "timer"


@pytest.mark.parametrize("phase", OWNED)
def test_signals_still_wake_sleeping_phases(home, cfg, phase):
    seed_phase(home, "T-1", phase)
    ops.requeue(cfg, "T-1", 100)
    base = snap_mod.load(home, "T-1").next_requeue_at
    spec = _due(home, "T-1", base + 1, current_spec_hash="CHANGED")
    assert spec.due and spec.reason == "spec-changed"
    assert disp.dispatch(cfg, DryRunSessions(), now=base + 1).spawned == []
    inbox.append_command(home, "T-1", "ans", {"qid": "q1", "text": "go"})
    assert disp.dispatch(cfg, DryRunSessions(), now=base + 1).spawned == ["T-1"]


def test_doctor_warns_on_checked_streak(home, cfg):
    seed_phase(home, "T-1", Phase.AWAITING_CI)
    for _ in range(11):
        ops.checked(cfg, "T-1")
    _, rep = run_doctor(home)
    chk = next(c for c in rep["checks"] if c["name"] == "checked_streak")
    assert chk["status"] == "ok"
    ops.checked(cfg, "T-1")
    _, rep = run_doctor(home)
    chk = next(c for c in rep["checks"] if c["name"] == "checked_streak")
    assert chk["status"] == "warn" and chk["counts"] == {"T-1": 12}
