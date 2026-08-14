"""The per-ticket state machine.

Explicit, enumerated phases with a documented transition table — one handler per
phase, never implicit markdown state. A ticket advances ONE phase per reconcile.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal


class Phase(str, Enum):
    TRIAGING = "triaging"            # newly seen; classify tier + scope
    AWAITING_HUMAN = "awaiting-human"  # blocked on an answer/approval (SLEEPING)
    READY = "ready"                  # approved + unblocked; waiting for a worker slot
    IMPLEMENTING = "implementing"    # ralph-loop running in a worktree
    QA = "qa"                        # adversarial review of a diff; may not Edit/Write
    RESEARCHING = "researching"      # research agent active in a worktree
    AWAITING_CI = "awaiting-ci"      # PR open; polling checks on a timer (SLEEPING)
    IN_REVIEW = "in-review"          # checks green; waiting on human/merge
    DEGRADED = "degraded"            # dead-lettered: repeated failure / non-convergence
    TERMINATING = "terminating"      # running finalizers (teardown)
    DONE = "done"                    # terminal


PhaseClass = Literal["active", "sleeping", "terminal"]

# RB-9: one exhaustive, hand-decided row per `Phase` -- the same house pattern
# as `_PHASE_COMMAND_SUFFIX`/`resolve_reconcile_command` (dispatcher.py:2321),
# `phase_denylist` (dispatcher.py:122) and `tier_denylist` (dispatcher.py:113).
# `SLEEPING_PHASES`/`TERMINAL_PHASES`/`ACTIVE_PHASES` below are *derived* from
# this table, never computed by subtraction -- subtraction is exactly what
# made a newly-added phase land in ACTIVE_PHASES by default with nobody having
# decided that, twice: 2026-07-19 (`in-review`, 21,731 no-op reconcilers,
# ~$845) and 2026-08-14 (`degraded`, 116 sessions, $8.63). `_assert_exhaustive`
# right below turns a missing row into a hard import-time failure instead of a
# silent slide into "active".
PHASE_CLASS: dict[Phase, PhaseClass] = {
    # newly seen; the dispatcher spawns a triage reconciler every sweep until
    # it classifies the ticket and moves it on -- there is no external signal
    # to wait on.
    Phase.TRIAGING: "active",
    # blocked on a human answer/approval; the reconciler exits and only the
    # dispatcher's inbox/timer/spec-hash checks in `is_due` wake it again.
    Phase.AWAITING_HUMAN: "sleeping",
    # approved and unblocked, waiting only for a worker slot -- nothing
    # external to poll, so it stays due until a session claims it.
    Phase.READY: "active",
    # the ralph-loop coding step runs to completion on every spawn.
    Phase.IMPLEMENTING: "active",
    # the adversarial review step runs to completion on every spawn.
    Phase.QA: "active",
    # the research step runs to completion on every spawn.
    Phase.RESEARCHING: "active",
    # PR open; the dispatcher's own `sync_vcs` tick polls CI directly and
    # advances the phase itself, so no reconciler needs to be due.
    Phase.AWAITING_CI: "sleeping",
    # checks green, waiting on human/merge; `sync_vcs` polls PR + review state
    # itself. This is the phase the 2026-07-19 runaway was fixed by adding --
    # the fix that prompted this table, so it stays here as the case in point.
    Phase.IN_REVIEW: "sleeping",
    # dead-lettered (repeated failure / non-convergence). Classified "active"
    # here ONLY to keep today's behavior byte-identical -- this is the known-
    # wrong default that produced the 2026-08-14 runaway (116 sessions, $8.63).
    # T-65 is the instance fix that reclassifies this to "sleeping"; RB-9 is
    # the class fix and deliberately does not change any existing
    # classification, so this row is a documented carry-over, not an
    # endorsement. Do not "fix" it here -- let T-65 land it.
    Phase.DEGRADED: "active",
    # runs finalizers (teardown) and must converge to DONE on its own, with no
    # external event to wait on -- unlike DEGRADED above, this one really was
    # audited: it was previously unclassified-by-luck (fell through the old
    # subtraction into ACTIVE_PHASES with nobody deciding it), and "active" is
    # the right call because finalizers need a reconciler to actually run them.
    Phase.TERMINATING: "active",
    # nothing left to do, ever; swept out of the active scan entirely.
    Phase.DONE: "terminal",
}


def _assert_exhaustive(phase_cls: type[Enum], classification: dict) -> None:
    """Raise if *classification* does not cover every member of *phase_cls* --
    the fail-closed partition check RB-9 exists to guarantee. Factored out of
    the module-load call below so a test can exercise the failure directly,
    against a throwaway enum, without needing to actually add an unclassified
    member to the real `Phase` (which would break everything else)."""
    missing = set(phase_cls) - set(classification)
    if missing:
        raise AssertionError(
            f"{phase_cls.__name__} member(s) have no PHASE_CLASS entry: "
            f"{sorted(m.value for m in missing)} -- add an explicit "
            "'active'/'sleeping'/'terminal' row with its reasoning before shipping."
        )


_assert_exhaustive(Phase, PHASE_CLASS)

# Phases where NO process is held: the reconciler exits and the dispatcher only
# re-wakes the ticket on a signal (inbox command, requeue timer, or spec edit).
# IN_REVIEW is sleeping too: the dispatcher's `sync_vcs` tick polls PR state, CI,
# and reviews directly and advances the phase itself — no reconciler needed.
SLEEPING_PHASES = frozenset(p for p, c in PHASE_CLASS.items() if c == "sleeping")

# Terminal phases are swept out of the active scan entirely.
TERMINAL_PHASES = frozenset(p for p, c in PHASE_CLASS.items() if c == "terminal")

# Active phases always have work to advance; the dispatcher spawns a worker
# whenever one is not already claimed by a live session.
ACTIVE_PHASES = frozenset(p for p, c in PHASE_CLASS.items() if c == "active")

# Allowed transitions — used to validate a PhaseChanged and to document the flow.
TRANSITIONS: dict[Phase, set[Phase]] = {
    Phase.TRIAGING: {Phase.AWAITING_HUMAN, Phase.READY, Phase.DEGRADED, Phase.TERMINATING},
    Phase.AWAITING_HUMAN: {Phase.READY, Phase.IMPLEMENTING, Phase.AWAITING_CI, Phase.TRIAGING,
                           Phase.RESEARCHING, Phase.DEGRADED, Phase.TERMINATING},
    Phase.READY: {Phase.IMPLEMENTING, Phase.RESEARCHING, Phase.AWAITING_HUMAN, Phase.DEGRADED, Phase.TERMINATING},
    Phase.IMPLEMENTING: {Phase.QA, Phase.AWAITING_CI, Phase.IN_REVIEW, Phase.DEGRADED,
                         Phase.AWAITING_HUMAN, Phase.TERMINATING, Phase.DONE},
    Phase.QA: {Phase.IMPLEMENTING, Phase.AWAITING_CI, Phase.DEGRADED,
              Phase.AWAITING_HUMAN, Phase.TERMINATING},
    Phase.RESEARCHING: {Phase.AWAITING_HUMAN, Phase.DEGRADED, Phase.TERMINATING, Phase.DONE},
    Phase.AWAITING_CI: {Phase.IMPLEMENTING, Phase.IN_REVIEW, Phase.AWAITING_HUMAN,
                        Phase.DEGRADED, Phase.TERMINATING, Phase.DONE},
    Phase.IN_REVIEW: {Phase.IMPLEMENTING, Phase.AWAITING_CI, Phase.AWAITING_HUMAN,
                      Phase.TERMINATING, Phase.DONE},
    Phase.DEGRADED: {Phase.READY, Phase.TRIAGING, Phase.TERMINATING},  # human can revive
    Phase.TERMINATING: {Phase.DONE},
    Phase.DONE: set(),
}


def can_transition(src: Phase, dst: Phase) -> bool:
    return dst in TRANSITIONS.get(src, set())
