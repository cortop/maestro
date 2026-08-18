# maestro — design

A per-ticket reconciler over append-only event streams. This document is the durable
rationale; `README.md` is the quickstart.

## Problem: the wave orchestrator

The predecessor pattern is a single skill that runs **waves 0→N strictly sequentially**
under a global `STATUS=running` mutex: collect answers → fetch tracker/VCS → rebuild a
monolithic dashboard → fan out implementers → surface questions → idle. Three structural
problems follow from that shape:

1. **Monolithic + blocking.** Every ticket moves in lockstep through the waves. The unit
   of execution is *the run*, not *the ticket*. One run can exceed 15 minutes, and the
   global mutex means nothing else can progress (and a crashed run wedges the lock).
2. **Unsafe concurrent edits.** The run does read-modify-**write** on shared global files
   (the dashboard, the question queue, the pending-work files). A human edit during the
   read→write window is a classic lost update — so you cannot safely touch state while a
   run is in flight.
3. **Bloated state.** Monolithic files grow to hundreds of KB and are re-read wholesale
   every run.

## Core idea

> **Per-ticket reconciler over append-only event streams.** Each ticket is a directory
> with its own append-only event log (sole source of truth) and a tiny folded snapshot
> (disposable cache). A cheap, level-triggered dispatcher sweeps snapshots, finds tickets
> whose observed state lags their desired state, and fans out one independent reconciler
> per due ticket. Each reconciler takes ONE idempotent step and exits. Humans are pure
> producers — they only append to inboxes or edit specs. Dashboards are projections.

This is the **Kubernetes controller / reconciliation** pattern (level-triggered, desired
vs observed, idempotent, per-object workqueue) on an **event-sourcing / CQRS** substrate
(append-only truth + regenerated read-models), with **single-writer-per-stream** +
**fencing tokens** for correctness and **virtual-actor**-style activation for lifecycle.
Human-in-the-loop is the **durable-workflow awakeable** ("sleep, wake on a signal")
realized on files + a cron clock instead of an always-on server.

## How each pain dies

- **Blocking →** no waves, no mutex. The dispatcher only enqueues-and-exits; up to N
  reconcilers run as independent OS-level `claude --bg` sessions. Wall-clock to drain the
  board is the slowest *single* ticket, not the sum of waves. A human question becomes
  "record `awaiting-human`, exit" — the slot frees immediately.
- **Unsafe edits →** write-ownership partition. Humans append to `inbox/<KEY>.jsonl` or
  edit `tickets/<KEY>/spec.md`; agents append to `events/<KEY>.jsonl` and atomically
  replace `derived/*`. The two never share a read-modify-write window, so a lost update is
  structurally impossible. A fencing token (optimistic concurrency on the log tail) guards
  the rare same-stream race.
- **Bloat →** a decision reads a ~1-2KB snapshot + new events; the dispatcher reads only
  snapshots. Compaction + archival keep it bounded forever.

## The Python / Claude split

The decisive implementation choice: **deterministic plumbing in Python, intelligence in
Claude.** The `maestro` package owns the correctness-critical machinery (fencing-gated
log, atomic writes, fold, idempotent step-ids, dispatcher, leases, projections,
dead-letter). Agents are `claude --bg` sessions that mutate state **only** through the
`maestro` CLI, so they cannot write a torn log or clobber a file. The dispatcher needs no
LLM at all, which is what makes an always-on fleet cheap.

## Components

| Component | Role | Harness primitive |
|-----------|------|-------------------|
| dispatcher (`maestro dispatch`) | level sweep: mint → due → spawn → exit; the sole fan-out point | launchd `StartInterval` (the durable clock; survives reboot) |
| reconciler (`/maestro-reconcile-<phase> <KEY>`) | one idempotent step per ticket | top-level `claude --bg` session, per-phase tool grant |
| Impl↔QA loop | `implementing` hands off to the independent `qa` phase; `qa` judges and routes back or onward | two REAL, separately-counted dispatcher spawns (`implementing`, `qa`), not subagents inside one reconciler — `qa` alone keeps the `Agent` tool, for at most one config-gated Standards-axis subagent (one legal fan-out level) |
| maestro CLI | correct-by-construction state verbs | Python (this package) |
| projector | snapshots → dashboards, atomic | a phase of `maestro dispatch` |
| providers | tracker / VCS / fetcher, pluggable | `config.toml` |

**Why not one Workflow session as the engine:** a single Workflow process is one failure
domain for all tickets — its crash kills the fleet. N independent `claude --bg` sessions
are isolated; one dying is one ticket, self-healed next sweep. The no-subagent-spawning
rule is honored because fan-out lives only at two top-level layers (dispatcher→reconcilers,
`qa` reconciler→its one Standards-axis subagent), never inside `implementing`.

## Acceptance criteria

A ticket's `## Acceptance criteria` are `- [ ]` checkbox lines in `spec.md`, each
identified by a content hash of its own line text (`snapshot.ac_hash`) — so a human's
edit to an AC's wording invalidates whatever was attested or checked against the old
text, rather than silently matching by index. Two evidence tiers gate `awaiting-ci`:

- **Self-attestation** (`maestro verify-ac`) — the implementer's own structured claim
  (what/where/result), independently re-checked by the `qa` phase's verdict. This is
  the default for prose ACs, and stays the only tier for a home with no `test_command`.
- **Machine-checked** (T-79, opt-in per AC) — a trailing annotation on the checkbox
  line, `(test: <path>[::<id>])` or `(check: <shell command>)`, makes that ONE AC
  provable by a subprocess instead of an attestation. With `test_command` configured
  (and the ticket not `mode: local`), the `verifying` stage (below) runs each
  annotated AC's own check at the same tree state as the suite and records it as an
  `AcCheckCaptured` event; a current-tree passing capture is what `awaiting-ci` then
  requires for that AC — `verify-ac` still records narrative evidence, it just stops
  being load-bearing. `test:` requires the named test (or, for a bare file path, some
  test in that file) to actually be part of the branch's diff against base and pass —
  the primitive that catches a green suite whose diff never added the test it claims
  to (the false-attestation failure mode this exists to close). Nothing here weakens
  independent QA: it still judges every AC, prose or annotated, and for an annotated
  one its job sharpens to auditing whether the named test's assertions actually match
  the AC's words. Unannotated ACs, a `test_command`-unset home, and a `mode: local`
  ticket are all unaffected — the annotation ships dark by construction, so the ~130
  pre-existing prose-only specs keep working untouched.

  T-84: `test:`'s added-test extraction and selector syntax (pytest `path::id`, `go
  test -run`, jest `-t`) are looked up per the ticket's repo binding `language`
  (`[repos.<name>] language`; `maestro/testlang.py` — unset means `"python"`, byte-
  identical to before this table existed). An unrecognized `language` fails
  `config.load()` closed, so a `test:` annotation can never fail-closed forever the way
  an unhandled language once could. `check:` performs no added-by-diff verification on
  any language, so it does not close the false-attestation class the way `test:` does.

  A green suite is a weak oracle for REMOVAL, not just addition: the `verifying` stage
  also diffs test NAMES (in each language's own test-file scope) against base once the
  suite passes. A net deletion routes to `awaiting-human` for a human sign-off instead
  of admitting `qa` — a rename never counts, and the sign-off is scoped to the exact
  tree state that asked for it (`test_deletion_gate`, default on).

## State machine

See [`docs/state-machine.md`](docs/state-machine.md) for the maintained phase diagram
(mermaid, generated from `maestro/statemachine.py`'s `TRANSITIONS`/`SLEEPING_PHASES`/
`TERMINAL_PHASES`/`ACTIVE_PHASES` by `make diagram`) and
[`docs/dispatch-gates.md`](docs/dispatch-gates.md) for the ordered dispatch gate table
(AST-walked from `dispatcher.py`). Both are derived, never retyped — a drift-guard test
(`tests/test_diagram.py`) fails `make test` if either goes stale.

`awaiting-human` and `awaiting-ci` are **sleeping** (no held process). The reconciler
records the gate + a `next_requeue_at`, then exits. The dispatcher re-wakes the ticket
when: the inbox has a new command, the requeue timer elapses, or the spec hash changed.
A fresh reconciler resumes by replaying the log — resumability comes from the event log we
already have, not a separate journal.

## Correctness guarantees (all tested)

- **Idempotency:** `step_id = hash(key, phase, observed_seq, action)`. Two reconcilers
  racing on the same frozen log compute the same id; the log dedups → one recorded effect.
- **Fencing:** an append with a stale `expected_last_seq` is rejected (optimistic
  concurrency); the loser bails and is re-derived next sweep. Armed (RB-7) on the
  state-machine gate, `ops.set_phase` — a caller that already folded the snapshot it
  decided the target phase from passes `expect=<that observed_seq>`; a lost race raises
  `StaleAppendError`, which is left to propagate uncaught (no `failure_count` spend, no
  dead-letter — see `ops.set_phase`'s docstring). Plain append-only event types (`Note`,
  `QuestionAsked`, …) don't carry it — nothing downstream branches on their freshness.
- **Crash safety:** a reconciler that dies after writing its event but before exit is
  re-spawned, recomputes the same step-id, and no-ops.
- **Inbox crash safety:** the reconciler acks the inbox cursor *last* (after advancing the
  phase), so a crash re-reads the same commands rather than dropping them.
- **Single writer per key + key isolation:** a per-key flock; distinct keys never contend.

## Concurrency & safety guardrails

idempotency keys · per-key claim (live-session dedup) + fencing-token authority ·
max-concurrency cap · jittered exponential backoff on failure · dead-letter after K
failures (one poison ticket can't wedge the fleet) · per-ticket circuit breaker
(`max_impl_turns`) · advisory token ceiling · finalizer teardown · append-only audit via
the event logs themselves · `dependsOn` gating to serialize coupled tickets.

## Migration from a wave orchestrator (strangler-fig)

Each step is independently shippable and reversible because the append-only logs are never
rewritten.

1. **Kill the clobber bug first (highest value, lowest risk).** Move all human input to
   append-only inboxes; make the dashboard a read-only projection. The wave engine keeps
   running unchanged. *(This is the entry point.)*
2. Per-ticket event logs + tiny snapshots → kills the big re-reads.
3. Stand up the dispatcher + one reconciler phase (start with `awaiting-ci` re-checks),
   DRY-RUN alongside the wave engine.
4. Move `triaging` + `awaiting-human` into the reconciler (sleep/wake goes live).
5. Cut over `implementing` (your existing Impl↔QA loop, raw worktrees, raise the cap).
   Add the launchd pin + a cloud-routine health-ping. Delete the global mutex + wave barrier.
6. Harden: dead-letter reaper, per-ticket budgets, finalizers, log compaction,
   `dependsOn` overlap auto-detection. This is where the real fleet's rate/spend/detection
   controls actually landed, driven by two incidents rather than foresight — see
   `docs/postmortem-2026-07-19.md` for the full control set and why each exists.

## SOTA sources

Reconciliation / controllers: kubernetes.io controllers & finalizers; controller-runtime
workqueue (idempotent reconcile, MaxConcurrentReconciles); level-triggering essays.
Durable execution / HITL: Temporal (signals, `wait_condition`), Restate awakeables,
Inngest wait-for-event, Azure Durable Entities/external-events, AWS Step Functions task
tokens, Cloudflare Durable Object alarms. Actors / concurrency: Orleans virtual actors
(turn-based, ETag), Akka mailboxes, the Single-Writer Principle, optimistic concurrency
(ETag/If-Match), lease + fencing tokens (Kleppmann). Agent frameworks: LangGraph
persistence + interrupts + Send, OpenAI Agents SDK handoffs, AutoGen save/load state,
CrewAI flows + @persist, Anthropic's multi-agent research system, Claude Agent SDK
sessions. Event sourcing / CQRS: Fowler + MS Architecture Center; outbox/inbox; Kafka log
compaction. Local-first files: POSIX durable write (fsync/rename/dir-fsync, macOS
F_FULLFSYNC), fswatch/watchman debouncing, git-worktrees-per-agent, SQLite-as-queue,
Automerge/Yjs CRDTs, Obsidian conflict-file mode. Pitfalls: thundering-herd + jittered
backoff, exactly-once myth, dead-letter / poison messages, saga compensation, agent-fleet
cost guardrails, oversight fatigue, correlation IDs.
