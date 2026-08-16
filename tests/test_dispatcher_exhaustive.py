"""RB-9: exhaustive enumeration of `dispatcher.is_due` and the `statemachine`
transition table. The 2026-07-19 runaway (21,731 no-op reconcilers, ~$845) was a
correct requeue-timer check sitting inside the wrong `if` branch -- a bug the
existing hand-picked-phase test would have caught only by luck. The input space
is finite (12 phases x a handful of flags), so this checks it completely instead
of sampling: 1152 points against the REAL `dispatcher.is_due`, in milliseconds.

Each property below is checked twice: once against the real, shipped code (must
hold), and once against a deliberately broken variant (must NOT hold) -- a guard
nobody has seen fail is a guard nobody knows works. See
`docs/formal-methods-evaluation.md` for why this replaces a formal-methods
approach for a domain this small.
"""
from __future__ import annotations

import itertools
from enum import Enum

import pytest

from maestro import dispatcher as disp
from maestro import statemachine
from maestro.snapshot import Snapshot
from maestro.statemachine import (
    ACTIVE_PHASES,
    PHASE_CLASS,
    SLEEPING_PHASES,
    TERMINAL_PHASES,
    TRANSITIONS,
    Phase,
)

# ---------------------------------------------------------------------------
# The grid: phase x inbox_pending x spec_changed x open_questions x
# answered_questions x blocked_dep x timer_state --
# 12 * 2 * 2 * 2 * 2 * 2 * 3 == 1152 points (RB-14 added the `verifying`
# phase, a 12th enum member, atop RF-6's 11-phase/1056-point count, which
# itself added `qa` atop the ticket's original 10-phase/960-point count).
# `blocked_dep` only ever affects the READY phase (dispatcher.py's
# `phase == Phase.READY and blocked_dep` line) but is included as a full axis
# rather than a separate narrower test, so it's covered by the same single
# exhaustive sweep as everything else.
# ---------------------------------------------------------------------------

_TIMER_STATES = ("none", "past", "future")
_NOW = 1_000_000.0
_SPEC_HASH = "h"

# Property 1's permitted exceptions: the three human-signal reasons plus the
# awaiting-human/stranded safety net (dispatcher.py:150-154) -- spelled out
# explicitly rather than skipped, per the ticket's Notes.
_ALLOWED_DUE_UNDER_FUTURE_TIMER = frozenset({"inbox", "spec-changed", "answered-pending", "stranded"})
_HUMAN_SIGNAL_REASONS = frozenset({"inbox", "spec-changed", "answered-pending"})


def _next_requeue_at(timer_state: str) -> float | None:
    if timer_state == "none":
        return None
    if timer_state == "past":
        return _NOW - 100
    return _NOW + 100


def all_points():
    """Every (phase, inbox_pending, spec_changed, open_q, answered_q,
    blocked_dep, timer_state) combination -- 1152 total."""
    return list(itertools.product(
        list(Phase), (False, True), (False, True), (False, True), (False, True),
        (False, True), _TIMER_STATES,
    ))


def snapshot_for(phase: Phase, open_q: bool, answered_q: bool, timer_state: str) -> Snapshot:
    return Snapshot(
        key="T-grid",
        phase=phase.value,
        spec_hash=_SPEC_HASH,
        open_questions={"q1": "real question?"} if open_q else {},
        answered_questions={"q1": "yes"} if answered_q else {},
        next_requeue_at=_next_requeue_at(timer_state),
    )


def evaluate(home, is_due_fn, point):
    phase, inbox_pending, spec_changed, open_q, answered_q, blocked_dep, timer_state = point
    snap = snapshot_for(phase, open_q, answered_q, timer_state)
    current_hash = "DIFFERENT" if spec_changed else _SPEC_HASH
    return is_due_fn(home, "T-grid", snap, inbox_pending=inbox_pending,
                     current_spec_hash=current_hash, now=_NOW, blocked_dep=blocked_dep)


@pytest.fixture(scope="module")
def grid_home(tmp_path_factory):
    # `is_due` never touches the filesystem at all (AD-7 removed the only spec
    # read it used to do, via `needs_approval` -> `spec_tier`) -- one shared,
    # empty home is enough for all 1152 points, no per-point construction
    # (that's the trap the ticket's Notes call out).
    return tmp_path_factory.mktemp("rb9-exhaustive-home")


@pytest.fixture(scope="module")
def grid(grid_home):
    """(point, DueResult) for all 1152 points, against the REAL `dispatcher.is_due`."""
    points = all_points()
    assert len(points) == 1152
    return [(p, evaluate(grid_home, disp.is_due, p)) for p in points]


def test_grid_covers_the_full_1152_point_space(grid):
    assert len(grid) == 1152


# ---------------------------------------------------------------------------
# Property 1: a pending FUTURE requeue timer holds every non-terminal phase.
# The $845 property -- the 2026-07-19 runaway.
# ---------------------------------------------------------------------------

def test_property_future_timer_holds_every_nonterminal_phase(grid):
    checked = 0
    for point, result in grid:
        phase, _, _, _, _, _, timer_state = point
        if timer_state != "future" or phase in TERMINAL_PHASES:
            continue
        checked += 1
        if result.due:
            assert result.reason in _ALLOWED_DUE_UNDER_FUTURE_TIMER, (point, result)
    assert checked == 11 * 2 * 2 * 2 * 2 * 2  # 11 non-terminal phases, timer fixed to "future"


def _pre_fix_is_due(home, key, snap, *, inbox_pending, current_spec_hash, now, blocked_dep=False):
    """Reconstruction of the pre-2026-07-19 bug: the requeue-timer check lived
    INSIDE the `if phase in SLEEPING_PHASES:` arm, so a reconciler that asked to
    sleep from an ACTIVE phase (`maestro requeue`, exactly what the in-review
    handler does) was ignored -- the ticket came back due on the very next
    sweep. Test-only; never imported by production code, and NOT a substitute
    for enumerating the real `is_due` above (see the ticket's Notes/TRAPS)."""
    phase = Phase(snap.phase)
    if phase in TERMINAL_PHASES:
        return disp.DueResult(False, "terminal")
    if inbox_pending:
        return disp.DueResult(True, "inbox")
    if phase == Phase.AWAITING_HUMAN and snap.answered_questions:
        return disp.DueResult(True, "answered-pending")
    if phase == Phase.AWAITING_HUMAN and not snap.open_questions and not snap.answered_questions:
        return disp.DueResult(True, "stranded")
    if current_spec_hash is not None and current_spec_hash != snap.spec_hash:
        return disp.DueResult(True, "spec-changed")
    if phase == Phase.READY and blocked_dep:
        return disp.DueResult(False, "blocked-dep")
    if phase in SLEEPING_PHASES:  # <-- bug: the timer check only runs in here
        if snap.next_requeue_at is not None:
            if snap.next_requeue_at > now:
                return disp.DueResult(False, "backoff")
            return disp.DueResult(True, "timer")
        return disp.DueResult(False, "sleeping")
    return disp.DueResult(True, "active")


def test_property_future_timer_has_teeth_against_prefix_nesting(grid_home):
    """Confirms property 1 would have caught the actual incident: a future
    timer set from an ACTIVE (non-sleeping) phase -- `implementing`, same as
    `in-review` was before it was folded into SLEEPING_PHASES -- is ignored by
    the pre-fix nesting -- exactly the 21,731-session runaway."""
    assert Phase.IMPLEMENTING not in SLEEPING_PHASES  # this demo needs a genuinely active phase
    point = (Phase.IMPLEMENTING, False, False, False, False, False, "future")
    result = evaluate(grid_home, _pre_fix_is_due, point)
    assert result.due and result.reason == "active"
    assert result.reason not in _ALLOWED_DUE_UNDER_FUTURE_TIMER  # <- what property 1 catches


# ---------------------------------------------------------------------------
# Property 2: a human signal is never throttled.
# ---------------------------------------------------------------------------

def test_property_human_signal_never_throttled(grid):
    due_true_reasons = {result.reason for _, result in grid if result.due}
    assert _HUMAN_SIGNAL_REASONS <= due_true_reasons  # all 3 are actually reachable
    assert disp._UNTHROTTLED_REASONS == _HUMAN_SIGNAL_REASONS
    machine_due_reasons = due_true_reasons - _HUMAN_SIGNAL_REASONS
    assert machine_due_reasons  # sanity: machine-originated due reasons exist too
    assert not (machine_due_reasons & disp._UNTHROTTLED_REASONS)


def test_property_human_signal_never_throttled_has_teeth():
    """A variant of `_UNTHROTTLED_REASONS` missing `answered-pending` would
    leave a reconciler that crashed between fold-inbox and set-phase (exactly
    the scenario dispatcher.py:146-149 exists for) sitting behind the spawn
    floor instead of finishing its transition immediately."""
    broken = frozenset({"inbox", "spec-changed"})  # answered-pending dropped
    assert not (_HUMAN_SIGNAL_REASONS <= broken)


# ---------------------------------------------------------------------------
# Property 3: a terminal phase is never due.
# ---------------------------------------------------------------------------

def test_property_terminal_phase_never_due(grid):
    checked = 0
    for point, result in grid:
        if point[0] in TERMINAL_PHASES:
            checked += 1
            assert not result.due and result.reason == "terminal", (point, result)
    assert checked == len(TERMINAL_PHASES) * 2 * 2 * 2 * 2 * 2 * 3  # 1 phase (DONE) -> 96


def test_property_terminal_phase_never_due_has_teeth(grid_home, monkeypatch):
    """Delete the real terminal-phase guard (dispatcher.py:142-143) and show a
    DONE ticket would spawn a reconciler forever. RB-9 added a second, later
    guard (the fail-closed ACTIVE_PHASES catch-all) that would also catch a
    DONE ticket once TERMINAL_PHASES is emptied here -- neutralize that one
    too, so this test still isolates the terminal-phase guard specifically
    rather than incidentally proving the newer guard's teeth instead."""
    monkeypatch.setattr(disp, "TERMINAL_PHASES", frozenset())
    monkeypatch.setattr(disp, "ACTIVE_PHASES", ACTIVE_PHASES | {Phase.DONE})
    snap = snapshot_for(Phase.DONE, False, False, "none")
    result = disp.is_due(grid_home, "T-grid", snap, inbox_pending=False,
                         current_spec_hash=_SPEC_HASH, now=_NOW)
    assert result.due  # bug: a finished ticket is due again


# ---------------------------------------------------------------------------
# Property 4: the reason determines dueness -- `is_due` is pure (same inputs ->
# same result), and each reason maps to exactly one dueness.
# ---------------------------------------------------------------------------

def test_property_is_due_is_deterministic(grid_home, grid):
    for point, result in grid:
        assert evaluate(grid_home, disp.is_due, point) == result, point


def test_property_reason_maps_to_exactly_one_dueness(grid):
    reason_to_dueness: dict[str, set] = {}
    for _, result in grid:
        reason_to_dueness.setdefault(result.reason, set()).add(result.due)
    for reason, duenesses in reason_to_dueness.items():
        assert len(duenesses) == 1, (reason, duenesses)


def test_property_reason_maps_to_one_dueness_has_teeth():
    """A reason meaning two different things -- due at one point, not-due at
    another -- is exactly the ambiguity this property forbids."""
    broken = {"active": {True, False}}
    with pytest.raises(AssertionError):
        for reason, duenesses in broken.items():
            assert len(duenesses) == 1, (reason, duenesses)


# ---------------------------------------------------------------------------
# Property 5: the transition table is complete: set(TRANSITIONS) == set(Phase).
# ---------------------------------------------------------------------------

def test_property_transition_table_is_complete():
    assert set(TRANSITIONS) == set(Phase)


def test_property_transition_table_complete_has_teeth():
    broken = dict(TRANSITIONS)
    del broken[Phase.DEGRADED]
    assert set(broken) != set(Phase)


# ---------------------------------------------------------------------------
# Property 6: every phase is reachable from `triaging`, and `DONE` is
# co-reachable from every phase -- no trap states, no unreachable states.
# ---------------------------------------------------------------------------

def _reachable_from(start: Phase, transitions: dict) -> set:
    seen = {start}
    stack = [start]
    while stack:
        cur = stack.pop()
        for nxt in transitions.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _reversed(transitions: dict) -> dict:
    rev: dict = {p: set() for p in transitions}
    for src, dsts in transitions.items():
        for dst in dsts:
            rev.setdefault(dst, set()).add(src)
    return rev


def test_property_every_phase_reachable_from_triaging():
    reachable = _reachable_from(Phase.TRIAGING, TRANSITIONS)
    assert reachable == set(Phase), set(Phase) - reachable


def test_property_done_co_reachable_from_every_phase():
    can_reach_done = _reachable_from(Phase.DONE, _reversed(TRANSITIONS))
    assert can_reach_done == set(Phase), set(Phase) - can_reach_done


def test_property_reachability_has_teeth_against_a_trap_state():
    """A phase with its outgoing edges wiped (a trap state) can no longer reach
    DONE -- co-reachability must catch it."""
    broken = {p: set(dsts) for p, dsts in TRANSITIONS.items()}
    broken[Phase.RESEARCHING] = set()
    can_reach_done = _reachable_from(Phase.DONE, _reversed(broken))
    assert Phase.RESEARCHING not in can_reach_done


def test_property_reachability_has_teeth_against_an_unreachable_phase():
    """A phase with every incoming edge removed can no longer be reached from
    `triaging` -- forward reachability must catch it."""
    broken = {p: (dsts - {Phase.RESEARCHING}) for p, dsts in TRANSITIONS.items()}
    reachable = _reachable_from(Phase.TRIAGING, broken)
    assert Phase.RESEARCHING not in reachable


# ---------------------------------------------------------------------------
# Property 7: the classification partitions the enum: SLEEPING and TERMINAL
# are disjoint, and SLEEPING | TERMINAL | ACTIVE == every phase. RB-9: these
# three sets are now *derived* from the explicit `PHASE_CLASS` table
# (statemachine.py), not computed by subtraction -- `_assert_exhaustive` fails
# the build at import time if any `Phase` member is missing a row, so an
# omission fails closed (never spawns) instead of silently landing in ACTIVE.
# ---------------------------------------------------------------------------

def test_property_classification_partitions_the_enum():
    assert SLEEPING_PHASES & TERMINAL_PHASES == set()
    assert SLEEPING_PHASES | TERMINAL_PHASES | ACTIVE_PHASES == set(Phase)
    # And it comes from the table, not a subtraction -- same partition, derived.
    assert PHASE_CLASS.keys() == set(Phase)
    assert SLEEPING_PHASES == {p for p, c in PHASE_CLASS.items() if c == "sleeping"}
    assert TERMINAL_PHASES == {p for p, c in PHASE_CLASS.items() if c == "terminal"}
    assert ACTIVE_PHASES == {p for p, c in PHASE_CLASS.items() if c == "active"}


def test_property_classification_partition_has_teeth():
    broken_sleeping = SLEEPING_PHASES | TERMINAL_PHASES  # DONE now claimed by both
    assert broken_sleeping & TERMINAL_PHASES != set()


def test_new_phase_without_classification_fails_the_build():
    """RB-9 AC2: adding a `Phase` member without a `PHASE_CLASS` decision must
    fail loudly, not silently land in ACTIVE_PHASES. Exercises the real
    `_assert_exhaustive` the module runs at import time -- against a throwaway
    enum with a temporary extra member left unclassified, so this proves the
    failure actually happens rather than merely asserting the intent."""

    class _FakePhase(str, Enum):
        KNOWN = "known"
        NEWLY_ADDED = "newly-added"  # <-- the temporary, deliberately unclassified member

    incomplete = {_FakePhase.KNOWN: "active"}
    with pytest.raises(AssertionError, match="newly-added"):
        statemachine._assert_exhaustive(_FakePhase, incomplete)


def test_new_phase_without_classification_has_teeth():
    """The counterpart: a *complete* classification must NOT raise -- otherwise
    the test above would pass for the wrong reason (any call raising)."""

    class _FakePhase(str, Enum):
        KNOWN = "known"

    complete = {_FakePhase.KNOWN: "active"}
    statemachine._assert_exhaustive(_FakePhase, complete)  # no raise


# ---------------------------------------------------------------------------
# Property 8: `is_due`'s final fallthrough is fail-closed, not active-by-
# default. RB-9 AC3: a phase that reaches the bottom of `is_due` without being
# in ACTIVE_PHASES must come back not-due, never fall through to "active".
# ---------------------------------------------------------------------------

def test_is_due_fallthrough_fails_closed_for_an_unclassified_phase(grid_home, monkeypatch):
    """Shrink ACTIVE_PHASES to exclude a phase that would otherwise reach the
    bottom of `is_due` (no inbox/timer gate applies) -- the real
    catch-all must report not-due, not silently fall through to "active" the
    way the pre-RB-9 unconditional `return DueResult(True, "active")` did."""
    monkeypatch.setattr(disp, "ACTIVE_PHASES", ACTIVE_PHASES - {Phase.IMPLEMENTING})
    snap = snapshot_for(Phase.IMPLEMENTING, False, False, "none")
    result = disp.is_due(grid_home, "T-grid", snap, inbox_pending=False,
                         current_spec_hash=_SPEC_HASH, now=_NOW)
    assert not result.due and result.reason == "unclassified"
