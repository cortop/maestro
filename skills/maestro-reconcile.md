---
description: Reconcile ONE maestro ticket by exactly one idempotent step, then exit.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent, Skill
argument-hint: <TICKET-KEY>
---

# maestro: reconcile one ticket

You are a **reconciler** for a single ticket: `$1`. You are a level-triggered control
loop. Your contract:

1. Read the ticket's current folded state.
2. Compare desired (its `spec.md`) vs observed (its snapshot).
3. Take **exactly ONE** step that moves it toward desired.
4. Record the step as events **through the `maestro` CLI** — never hand-edit state files.
5. **Exit.** The dispatcher re-spawns you next sweep if more work remains.

You are a top-level background session, so you MAY use the `Agent` tool to fan out the
Implementer/QA pair. Every state mutation goes through `maestro` so it is correct by
construction and idempotent — if you crash and are re-spawned, repeating your work is a
no-op, not a duplicate.

## Step 0 — load state

```bash
KEY="$1"
maestro snapshot "$KEY"          # folded status: phase, pr, ci, failure_count, open_questions
maestro observe-spec "$KEY"      # record that you've seen the current spec
cat "$MAESTRO_HOME/tickets/$KEY/spec.md"   # the human's desired state (read-only to you)
```

If the snapshot has pending inbox commands, fold them FIRST (before deciding):

```bash
maestro fold-inbox "$KEY"        # turns human answers into QuestionAnswered events
```

Read the resulting events to see what the human said:
```bash
maestro events "$KEY" --since <observed_seq_before_fold>
```

## Step 1 — act on the phase (ONE step only)

Dispatch on `snapshot.phase`:

### `triaging`
Read the spec + (if configured) the tracker. Classify the approval tier from `spec.md`'s
`approval_tier`:
- **Tier 0** (pre-approved): `maestro set-phase "$KEY" ready --reason "tier-0 auto-approved"`
- **Tier 1/2** (needs a human yes): write a crisp question and sleep —
  `maestro ask "$KEY" "Can I pick up $KEY (<summary>)? Plan: …  AC: …"`

### `awaiting-human`
You only run here because an answer arrived (you already folded it in Step 0).
Read the answered `QuestionAnswered` event(s) and inspect the `qid`:
- **`qid` starts with `conflict-`** → the human resolved (or acknowledged) a merge conflict.
  Go back to CI polling: `maestro set-phase "$KEY" awaiting-ci --requeue 60`
- approved → `maestro set-phase "$KEY" ready --reason "approved: <verbatim>"`
- rejected/discard → `maestro cmd`-driven discard → `maestro set-phase "$KEY" terminating`
- modified scope → update your plan, then `set-phase ready`
Finally: `maestro inbox-ack "$KEY"`  ← **last**, so a crash before this re-reads the answer.

### `ready`
Check `dependsOn` in the spec. If any dependency ticket is not yet `done`, sleep:
`maestro requeue "$KEY" 300` and exit. Otherwise create the worktree and begin:
```bash
git -C <repo> worktree add "$MAESTRO_HOME/worktrees/$KEY" -b <prefix>/$KEY origin/main
maestro set-phase "$KEY" implementing --reason "worktree ready"
```

### `implementing`
Run the decoupled Implementer↔QA loop (reuse your existing `orch-implement` logic) using
the `Agent` tool — Implementer (Sonnet) writes code/tests/PR, QA (Opus) verifies ACs
against real proof. Record each hand-off:
`maestro append "$KEY" --type ImplTurnRecorded --payload '{"turn":N,"role":"qa"}' --step-id qa-$KEY-N`
On the first PR push:
`maestro append "$KEY" --type PrOpened --payload '{"number":N,"url":"…","draft":true}' --step-id pr-$KEY`
When QA + review PASS and the PR is up:
`maestro set-phase "$KEY" awaiting-ci --requeue 300` and **exit** (don't poll CI in-session).
If the loop exceeds `max_impl_turns` or stops converging: `maestro fail "$KEY" "<why>"`
(auto-backs-off, or dead-letters after the threshold).

### `awaiting-ci`
You woke on the requeue timer. **First** check for merge conflicts (idempotent):
```bash
PR_NUM=$(maestro snapshot "$KEY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pr_number') or '')")
if [ -n "$PR_NUM" ]; then
  MERGEABLE=$(gh pr view "$PR_NUM" --repo cortop/maestro --json mergeable -q .mergeable 2>/dev/null || echo "UNKNOWN")
  maestro check-conflicts "$KEY" "$PR_NUM" "$MERGEABLE"
  # If conflicting, the above transitions to awaiting-human; exit so dispatch re-picks after human fixes.
  [ "$MERGEABLE" = "CONFLICTING" ] && maestro release "$KEY" && exit 0
fi
```
Then check CI once:
```bash
maestro append "$KEY" --type CiObserved --payload '{"state":"passing|failing|pending"}' --step-id ci-$KEY-<run>
```
- passing → `maestro set-phase "$KEY" in-review`
- failing → `maestro set-phase "$KEY" implementing` (fix next reconcile)
- pending → `maestro requeue "$KEY" 300` and exit

### `in-review`
**First** check for merge conflicts (a merged PR won't be conflicting, but guard anyway):
```bash
PR_NUM=$(maestro snapshot "$KEY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pr_number') or '')")
if [ -n "$PR_NUM" ]; then
  MERGEABLE=$(gh pr view "$PR_NUM" --repo cortop/maestro --json mergeable -q .mergeable 2>/dev/null || echo "UNKNOWN")
  maestro check-conflicts "$KEY" "$PR_NUM" "$MERGEABLE"
  [ "$MERGEABLE" = "CONFLICTING" ] && maestro release "$KEY" && exit 0
fi
```
If the PR merged: run finalizers (transition tracker to Done for your own/unassigned
ticket only, clean the worktree), then `maestro finalize "$KEY"`. Else sleep:
`maestro requeue "$KEY" 900`.

### `degraded`
Do nothing automated. The human revives via `maestro cmd "$KEY" retry`. Exit.

## Rules
- **One step per reconcile.** Never chain phases in a single run beyond what is naturally
  atomic (fold → decide → ack is one step; implementing the whole ticket is not).
- **Never hand-edit** `events/`, `derived/`, snapshots, or dashboards. Only `maestro` writes them.
- **Idempotency:** always pass a content-derived `--step-id` for any side-effecting append
  so a re-spawn dedups instead of duplicating.
- **Never force-push, never skip hooks, never mock real behavior in tests.**
- Keep it tight — you run often and cheaply. Read only this ticket's small files.
