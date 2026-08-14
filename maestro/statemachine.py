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
# DEGRADED is sleeping too (OC-7): it means "parked for a human", not "always has
# work to advance" -- a ticket dead-lettered by `ops.fail` (repeated failure or
# non-convergence) has nothing left for any reconciler to do until a human acts.
# Before this, DEGRADED sat in ACTIVE_PHASES, so `is_due` kept saying "active"
# forever: the dispatcher respawned the passive reconciler every sweep, it
# correctly found nothing to route and exited without appending, `_allow_spawn`
# counted that as no-progress, and once `max_spawn_attempts` tripped it called
# `ops.fail` again -- which, already past `max_failures`, re-appended another
# Failed/Stalled pair on THIS call and returned without ever setting a backoff
# timer, so the ticket came right back due next sweep. Measured on the dogfood
# board 2026-08-14: 116 reconciler sessions / $8.63 across two tickets in under
# 7 hours, zero progress (see T-65's spec Notes). Sleeping here closes the loop
# at its source: a degraded ticket is due again only via the same signals any
# other sleeping phase gets -- `inbox_pending` (a human `maestro cmd <KEY> retry`
# or `maestro cmd <KEY> discard` lands in the inbox and wakes it on the very
# next sweep, independent of this set, exactly like AWAITING_HUMAN today) or a
# spec edit (`current_spec_hash` mismatch, same mechanism). No new revival path
# needed -- both already exist in `dispatcher.is_due` above the SLEEPING_PHASES
# check and were unaffected by this change.
SLEEPING_PHASES = frozenset({Phase.AWAITING_HUMAN, Phase.AWAITING_CI, Phase.IN_REVIEW,
                              Phase.DEGRADED})

# Terminal phases are swept out of the active scan entirely -- no reconciler is
# ever spawned again and no signal (inbox, spec edit, timer) wakes them; the
# only way out is a fresh ticket. DEGRADED is deliberately NOT here even though
# it is a dead-letter: `TRANSITIONS[Phase.DEGRADED]` allows READY/TRIAGING/
# TERMINATING, i.e. it is reachable again given the right human signal, so it
# belongs in SLEEPING_PHASES (parked, revivable) rather than TERMINAL_PHASES
# (permanently swept out, no way back short of a new ticket).
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
