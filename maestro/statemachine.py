"""The per-ticket state machine.

Explicit, enumerated phases with a documented transition table — one handler per
phase, never implicit markdown state. A ticket advances ONE phase per reconcile.
"""
from __future__ import annotations

from enum import Enum


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


# Phases where NO process is held: the reconciler exits and the dispatcher only
# re-wakes the ticket on a signal (inbox command, requeue timer, or spec edit).
# IN_REVIEW is sleeping too: the dispatcher's `sync_vcs` tick polls PR state, CI,
# and reviews directly and advances the phase itself — no reconciler needed.
SLEEPING_PHASES = frozenset({Phase.AWAITING_HUMAN, Phase.AWAITING_CI, Phase.IN_REVIEW})

# Terminal phases are swept out of the active scan entirely.
TERMINAL_PHASES = frozenset({Phase.DONE})

# Active phases always have work to advance; the dispatcher spawns a worker
# whenever one is not already claimed by a live session.
ACTIVE_PHASES = frozenset(
    set(Phase) - SLEEPING_PHASES - TERMINAL_PHASES
)

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
