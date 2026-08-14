---
description: Reconcile a dispatcher-owned or teardown-only maestro ticket (awaiting-ci, in-review, degraded, terminating). (maestro self-dev)
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` — passive phases (self-development)

You are the reconciler for ticket **`$1`** of the maestro project, spawned because it is
currently in `awaiting-ci`, `in-review`, `degraded`, or `terminating` — phases with no active
implementation work for a reconciler to do. Record any state change only through the `maestro`
CLI, then exit.

## Always: load state first
```bash
KEY="$1"
maestro env               # -> home (board-wide; keyless -- no bound repo to resolve here)
maestro observe-spec "$KEY"
maestro snapshot "$KEY"   # -> phase, pr, ci, failure_count, open_questions
```
Read the JSON above and hold `home` as MHOME for the rest of this file — no shell variable
assignment; this preamble runs no `eval`, `python3`, `sed`, or `cat`.

Finish every exit path with `maestro release "$KEY"` (drop your claim).

## Always: drain the inbox before anything else
A pending inbox command is a wake signal in EVERY phase — `dispatcher.is_due` checks
`inbox_pending` *before* the sleeping-phase check and *before* the backoff timer, so a command
left unacked here re-wakes the ticket on every single sweep, forever, and defeats backoff
entirely. These phases are the dangerous ones: nothing else about a sleeping ticket changes
between sweeps, so an unacked command is an unbounded no-op spawn loop (this is exactly the
2026-08-07 T-24 spin, 17 spawns/hour). Draining it is this reconciler's real job.
```bash
maestro fold-inbox "$KEY"   # -> records CommandReceived (+ QuestionAnswered for answers)
```
Read the folded commands (`maestro show "$KEY"` → `pending_inbox`) and route by intent — a
human who wrote to a sleeping ticket wants *something*, and silently swallowing it is as wrong
as spinning on it:

- **asks for a change to the work** (scope, a fix, "it should also…") → send it back to be
  implemented, carrying the message verbatim so the implementer sees the actual words:
  `maestro set-phase "$KEY" implementing --reason "human: <verbatim message>"`
- **`retry` on a `degraded` ticket** → `maestro set-phase "$KEY" ready --reason "human retry"`
- **stop / discard** → `maestro set-phase "$KEY" terminating --reason "human: <verbatim>"`
- **needs a decision you cannot make** → `maestro ask "$KEY" "<question>"` (this parks the
  ticket in `awaiting-human`, which is a real wait, not a spin)
- **purely informational** (an acknowledgement, a note with no ask) → append it and move on:
  `maestro append "$KEY" --type Note --payload "{\"text\":\"<verbatim>\"}" --step-id "note-inbox-$KEY-<idx>"`

**Then, last on any path that folded commands**, advance the cursor:
```bash
maestro inbox-ack "$KEY"
```
Ack last, after the phase change has appended — a crash before the ack re-reads the same
commands next sweep, which is safe; acking first would lose them.

If `maestro show "$KEY"` reports no `pending_inbox`, skip this whole section and fall through
to the phase branch below.

## `awaiting-ci` / `in-review`: dispatcher-owned, sleeping
Both are sleeping phases the dispatcher's `sync_vcs` tick (see `maestro/dispatcher.py`) owns:
every sweep it polls PR state, CI checks, and review comments directly via the configured `vcs`
provider and advances the phase itself — merged finalizes (+ removes the worktree), a
CONFLICTING PR routes to `implementing` for auto-resolution, failing CI routes to `implementing`
with the failing check names in the reason, passing CI moves `awaiting-ci` → `in-review`, and a
CHANGES_REQUESTED review routes back to `implementing` with the verbatim comment body. So if
the inbox was empty, you were spawned on a stray signal and there is nothing to do — say so
before releasing (RB-10: a completed no-op must be distinguishable from a crash, see below):
```bash
maestro checked "$KEY"
maestro release "$KEY"
```
The one thing that *is* yours here is a human command — the dispatcher polls the PR, not the
inbox, so nothing else will ever read it. Handle it per the drain section above before
releasing.

**Done when:** either the inbox was empty and `maestro checked "$KEY"` then `maestro release
"$KEY"` ran, or every pending command was folded, routed (one `set-phase`/`ask`/`append`),
`maestro inbox-ack "$KEY"` ran last, and `maestro release "$KEY"` ran.

## `degraded`: dead-lettered, waiting on a human
Repeated failure or non-convergence sent this ticket here. The revival signal is a human
running `maestro cmd "$KEY" retry` — which lands in the inbox, so **you** are the one who acts
on it (the drain section above routes it to `ready`). With no pending command there is nothing
automated to do — this is the exact shape of the 2026-08-14 T-55 incident (RB-10's Notes): say
so, so the no-progress watchdog counts this sweep as a healthy check-in, not a crash:
```bash
maestro checked "$KEY"
maestro release "$KEY"
```
`degraded` is an *active* phase, so an unacked command spins here just as hard as in a sleeping
one — ack it.

**Done when:** either the inbox was empty and `maestro checked "$KEY"` then `maestro release
"$KEY"` ran, or a `retry` was folded, `set-phase … ready` appended, `maestro inbox-ack "$KEY"`
ran last, and `maestro release "$KEY"` ran.

## `terminating`: finish the teardown
The ticket was routed here (e.g. `awaiting-human` recorded a `discard`/rejection). Finish it:
```bash
maestro finalize "$KEY"
maestro inbox-ack "$KEY"   # if anything was folded — `done` is terminal and never re-wakes,
                           # so an unacked command here is lost silently rather than re-read
maestro release "$KEY"
```
**Done when:** `Finalized` has appended (phase now reads `done`), `maestro inbox-ack "$KEY"`
has run if commands were folded, and `maestro release "$KEY"` has run.
