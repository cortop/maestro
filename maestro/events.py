"""Event types — the vocabulary of the append-only log.

The event log (``events/<KEY>.jsonl``) is the *sole source of truth* for a ticket.
Everything else (snapshots, dashboards) is a disposable projection folded from it.
"""
from __future__ import annotations

# Lifecycle
TICKET_CREATED = "TicketCreated"        # {title, source, spec_hash, repo}
SPEC_OBSERVED = "SpecObserved"          # {spec_hash}  reconciler folded the current spec
PHASE_CHANGED = "PhaseChanged"          # {phase, reason, forced_by?}  forced_by: actor who used --force past the AC gate
FINALIZED = "Finalized"                 # {}  tombstone -> swept to archive

# Human-in-the-loop
QUESTION_ASKED = "QuestionAsked"        # {qid, text}
QUESTION_ANSWERED = "QuestionAnswered"  # {qid, answer}
COMMAND_RECEIVED = "CommandReceived"    # {command, args}  folded from the inbox

# Implementation / VCS
PR_OPENED = "PrOpened"                  # {number, url, draft}
PR_UPDATED = "PrUpdated"                # {number, draft, merged}
CI_OBSERVED = "CiObserved"              # {state, failing_checks, detail, error?}  state: "passing"|"failing"|"pending"|"unknown"
                                         # error (optional): "auth"|"not_found"|"unknown" -- WHY a poll couldn't read
                                         # the PR (state stays "unknown"); a "transient" classification is a free
                                         # retry and appends no event at all. See providers/base.py VCS.pr_status.
REVIEW_FEEDBACK_RECEIVED = "ReviewFeedbackReceived"  # {comment_id, state, body, author}  one PR review; idempotent per comment_id
IMPL_TURN = "ImplTurnRecorded"          # {turn, role}  one Implementer/QA hand-off
IMPL_STEP = "ImplStepRecorded"          # {turn, role, kind, tool, summary}  one notable stream step

# Self-review
AC_VERIFIED = "AcVerified"              # {ac_hash, ac_index, ac_text, evidence}  evidence: {what, where, result}; content-hash keyed

# Test-run gate (RB-12/RB-14): maestro's OWN captured proof the suite passes at
# a given tree state -- produced only by a real subprocess call (ops.capture_tests,
# or RB-14's dispatcher-owned async equivalent, dispatcher.sync_test_runs), never
# an agent's self-attestation. See ops._tests_stale_reason (whether the current
# tree state already has a satisfying record) and snapshot.Snapshot.test_runs
# (keyed by tree_key: HEAD sha + a hash of the dirty tree, so a stale pass never
# satisfies a changed tree).
TEST_RUN_CAPTURED = "TestRunCaptured"   # {tree_key, command, exit_code, passed, failure_excerpt?}
                                         # failure_excerpt (RB-14): a bounded tail of the
                                         # run's combined stdout+stderr, present only when
                                         # exit_code != 0 -- what a red VERIFYING routes back
                                         # to `implementing` with, so the next reconciler
                                         # starts from the actual failure, never re-derives it.

# Approval
# AD-7: historical-only -- `maestro approve` and the tier-2 implementing gate it
# cleared are gone (30 existing event logs carry a real Approved event, and the
# log is append-only, so `snapshot.fold` must keep parsing this forever even
# though nothing emits it anymore).
APPROVED = "Approved"                   # {}

# Independent QA (a separate agent that did not write the code re-checks the diff)
AC_QA_VERDICT = "AcQaVerdict"           # {ac_hash, ac_index, ac_text, verdict, evidence, axis}
                                         # verdict: "pass"|"fail"; axis: "spec"|"standards" (T-23),
                                         # default "spec" when omitted (pre-T-23 events). The two
                                         # axes are folded into separate snapshot buckets and never
                                         # reranked against each other -- only the "spec" axis gates
                                         # `implementing -> awaiting-ci` (see ops._refuse_if_qa_failing);
                                         # a "standards" verdict is advisory and recorded only.

# Research
RESEARCH_PROPOSED = "ResearchProposed"  # {proposal_path, alternatives}  not side-effecting; file write is content-idempotent

# External sync (opt-in trackers, e.g. Jira, Linear)
JIRA_SYNCED = "JiraSynced"              # {jira_updated_ts, status, last_comment_id}
LINEAR_SYNCED = "LinearSynced"          # {linear_updated_ts, status, last_comment_id}

# Control
REQUEUE_SCHEDULED = "RequeueScheduled"  # {at}  epoch seconds to re-wake a sleeping ticket
FAILED = "Failed"                       # {error, kind?}  increments failure_count
STALLED = "Stalled"                     # {reason, kind?}  -> DEGRADED / dead-letter
                                         # kind="burn" (RB-11): parked by burn.should_park,
                                         # not a generic failure -- see snapshot.Snapshot.burning
NOTE = "Note"                           # {text}  free-form audit breadcrumb
CHECKED = "Checked"                     # {}  reconciler ran to completion, correctly found
                                         # nothing due -- advances observed_seq so the
                                         # no-progress watchdog (`_allow_spawn`) can tell this
                                         # apart from a crash (RB-10). Cheapest possible payload
                                         # by design; see ops.checked for the cost accounting.

# Side-effecting events whose presence (by step_id) means the external action
# already happened — used to make re-spawn after a crash idempotent.
SIDE_EFFECTING = frozenset({PR_OPENED, PR_UPDATED, FINALIZED, QUESTION_ASKED})
