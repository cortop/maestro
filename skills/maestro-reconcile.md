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
Read `answered_questions` from the snapshot — it persists across crashes, so it's reliable
even if `observed_seq` has already advanced past the `QuestionAnswered` events:
```bash
SNAP=$(maestro snapshot "$KEY")
```
Inspect each qid in `answered_questions`:
- **any qid starts with `conflict-`** → an escalated merge conflict the agent could not auto-resolve;
  the human gave guidance. Re-enter the worktree to apply it and retry the resolution:
  `maestro set-phase "$KEY" implementing --reason "retry conflict resolution: <verbatim>"`
- **approval** (answer text is affirmative / qid is not conflict-) → `maestro set-phase "$KEY" ready --reason "approved: <verbatim>"`
- **rejected/discard** → `maestro set-phase "$KEY" terminating`
- **modified scope** → note it, then `set-phase ready`
Finally: `maestro inbox-ack "$KEY"`  ← **last**, so a crash before this re-reads the answer.

If `answered_questions` AND `open_questions` are **both empty**, you were woken as `stranded`
(awaiting-human with nothing to wait on — the dispatcher wakes these so they can't sleep
forever). Recover so you make progress this step: if `pr_number` is set →
`maestro set-phase "$KEY" awaiting-ci --requeue 60`, else `maestro set-phase "$KEY" triaging --reason "stranded recovery"`.

### `ready`
Check `dependsOn` in the spec. If any dependency ticket is not yet `done`, sleep:
`maestro requeue "$KEY" 300` and exit. Otherwise create the worktree and begin:
```bash
git -C <repo> worktree add "$MAESTRO_HOME/worktrees/$KEY" -b <prefix>/$KEY origin/main
maestro set-phase "$KEY" implementing --reason "worktree ready"
```

### `implementing`
**If you landed here to resolve a conflict** (snapshot `reason` mentions a merge conflict, and
a PR is already open — `pr_number` set), do ONLY that: in the worktree, `git fetch origin main`,
`git rebase origin/main`, resolve the conflicts, run the tests, push the rebased branch (never
force-push onto someone else's work without care), then `maestro set-phase "$KEY" awaiting-ci
--requeue 300`. Do not re-implement the feature. If you cannot resolve it, escalate with
`maestro ask "$KEY" "PR #<n> conflict I couldn't auto-resolve: <detail>" --qid "conflict-$KEY-<n>"`.

Otherwise run the decoupled Implementer↔QA loop (reuse your existing `orch-implement` logic) using
the `Agent` tool — Implementer (Sonnet) writes code/tests/PR, QA (Opus) verifies ACs
against real proof. **"Real proof" = the feature demonstrated against the real app, not mocks
of the component under test:** drive the real `maestro` CLI / a real dispatcher sweep
(`DryRunSessions`) over a temp home and assert observable state. For TUI changes (`tui*.py`)
that means mounting `MaestroTUI` via `async with app.run_test() as pilot:` in
`tests/test_tui_runtime.py` (drive keys, assert `app._exception is None`, extend the binding
sweep). Mocking the runtime/CLI under test does not count, and QA must reject it. Install
`.[dev,tui]` so those tests run rather than skip. Record each hand-off:
`maestro append "$KEY" --type ImplTurnRecorded --payload '{"turn":N,"role":"qa"}' --step-id qa-$KEY-N`
On the first PR push:
`maestro append "$KEY" --type PrOpened --payload '{"number":N,"url":"…","draft":true}' --step-id pr-$KEY`
When QA + review PASS and the PR is up:
`maestro set-phase "$KEY" awaiting-ci --requeue 300` and **exit** (don't poll CI in-session).
If the loop exceeds `max_impl_turns` or stops converging: `maestro fail "$KEY" "<why>"`
(auto-backs-off, or dead-letters after the threshold).

### `awaiting-ci`
You woke on the requeue timer. **First** check if the PR is already merged or has conflicts:
```bash
PR_NUM=$(maestro snapshot "$KEY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pr_number') or '')")
if [ -n "$PR_NUM" ]; then
  PR_STATE=$(gh pr view "$PR_NUM" --repo cortop/maestro --json state -q .state 2>/dev/null | tr '[:lower:]' '[:upper:]' || echo "UNKNOWN")
  if [ "$PR_STATE" = "MERGED" ]; then
    git -C "$REPO" worktree remove "$HOME/worktrees/$KEY" --force 2>/dev/null || true
    maestro check-merged "$KEY" "MERGED"
    maestro release "$KEY" && exit 0
  fi
  MERGEABLE=$(gh pr view "$PR_NUM" --repo cortop/maestro --json mergeable -q .mergeable 2>/dev/null || echo "UNKNOWN")
  maestro check-conflicts "$KEY" "$PR_NUM" "$MERGEABLE"
  # If CONFLICTING, check-conflicts routes to implementing so the agent rebases & pushes
  # (auto-resolution); exit so the implementing reconcile picks it up.
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
