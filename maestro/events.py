"""Event types — the vocabulary of the append-only log.

The event log (``events/<KEY>.jsonl``) is the *sole source of truth* for a ticket.
Everything else (snapshots, dashboards) is a disposable projection folded from it.
"""
from __future__ import annotations

# Lifecycle
TICKET_CREATED = "TicketCreated"        # {title, source, spec_hash}
TICKET_TRIAGED = "TicketTriaged"        # {tier, classification, phase}
SPEC_OBSERVED = "SpecObserved"          # {spec_hash}  reconciler folded the current spec
PHASE_CHANGED = "PhaseChanged"          # {phase, reason}
FINALIZED = "Finalized"                 # {}  tombstone -> swept to archive

# Human-in-the-loop
QUESTION_ASKED = "QuestionAsked"        # {qid, text}
QUESTION_ANSWERED = "QuestionAnswered"  # {qid, answer}
COMMAND_RECEIVED = "CommandReceived"    # {command, args}  folded from the inbox

# Implementation / VCS
PR_OPENED = "PrOpened"                  # {number, url, draft}
PR_UPDATED = "PrUpdated"                # {number, draft, merged}
CI_OBSERVED = "CiObserved"              # {state}  e.g. "passing" | "failing" | "pending"
IMPL_TURN = "ImplTurnRecorded"          # {turn, role}  one Implementer/QA hand-off
IMPL_STEP = "ImplStepRecorded"          # {turn, role, kind, tool, summary}  one notable stream step

# Control
REQUEUE_SCHEDULED = "RequeueScheduled"  # {at}  epoch seconds to re-wake a sleeping ticket
FAILED = "Failed"                       # {error}  increments failure_count
STALLED = "Stalled"                     # {reason}  -> DEGRADED / dead-letter
NOTE = "Note"                           # {text}  free-form audit breadcrumb

# Side-effecting events whose presence (by step_id) means the external action
# already happened — used to make re-spawn after a crash idempotent.
SIDE_EFFECTING = frozenset({PR_OPENED, PR_UPDATED, FINALIZED, QUESTION_ASKED})
