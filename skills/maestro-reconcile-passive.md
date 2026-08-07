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
eval "$(maestro env | python3 -c 'import sys,json;print("HOME="+json.load(sys.stdin)["home"])')"
maestro observe-spec "$KEY"
maestro snapshot "$KEY"   # -> phase, pr, ci, failure_count, open_questions
```
Finish every exit path with `maestro release "$KEY"` (drop your claim).

## `awaiting-ci` / `in-review`: dispatcher-owned, sleeping
Both are sleeping phases the dispatcher's `sync_vcs` tick (see `maestro/dispatcher.py`) owns:
every sweep it polls PR state, CI checks, and review comments directly via the configured `vcs`
provider and advances the phase itself — merged finalizes (+ removes the worktree), a
CONFLICTING PR routes to `implementing` for auto-resolution, failing CI routes to `implementing`
with the failing check names in the reason, passing CI moves `awaiting-ci` → `in-review`, and a
CHANGES_REQUESTED review routes back to `implementing` with the verbatim comment body. You
should essentially never be spawned into either — if you land here anyway (a stray signal),
there is nothing to do:
```bash
maestro release "$KEY"
```
**Done when:** `maestro release "$KEY"` has run — no other state change is expected.

## `degraded`: dead-lettered, waiting on a human
Repeated failure or non-convergence sent this ticket here. Nothing automated to do — wait for
a human to run `maestro cmd "$KEY" retry`.
```bash
maestro release "$KEY"
```
**Done when:** `maestro release "$KEY"` has run — no other state change is expected.

## `terminating`: finish the teardown
The ticket was routed here (e.g. `awaiting-human` recorded a `discard`/rejection). Finish it:
```bash
maestro finalize "$KEY"
maestro release "$KEY"
```
**Done when:** `Finalized` has appended (phase now reads `done`) and `maestro release "$KEY"`
has run.
