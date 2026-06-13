---
description: Reconcile ONE maestro ticket by exactly one idempotent step, then exit. (maestro self-dev)
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` (self-development)

You are the reconciler for ticket **`$1`** of the maestro project. You take **exactly ONE**
step toward its desired state, record it **only through the `maestro` CLI**, then **exit**.
The dispatcher re-spawns you next sweep if more remains. You are a top-level session, so you
may use the `Agent` tool. Every state write goes through `maestro` so it is idempotent under
crash-and-respawn.

## Always: load state first
```bash
KEY="$1"
eval "$(maestro env | python3 -c 'import sys,json;d=json.load(sys.stdin);print(f"REPO={d[\"repo_path\"]}\nPREFIX={d[\"branch_prefix\"]}\nHOME={d[\"home\"]}")')"
maestro observe-spec "$KEY"
maestro snapshot "$KEY"                     # -> phase, pr, ci, failure_count, open_questions
sed -n '1,200p' "$HOME/tickets/$KEY/spec.md"   # desired state (you never edit this)
```
If the snapshot shows pending inbox commands, fold them BEFORE deciding:
```bash
maestro fold-inbox "$KEY"
```
**On every exit path below, finish with `maestro release "$KEY"`** (drop your claim).

## Act on `snapshot.phase` — ONE step only

### `triaging`
Read the spec. Take `approval_tier` from its frontmatter.
- tier 0 → `maestro set-phase "$KEY" ready --reason "tier-0 auto-approved"`
- tier ≥1 → write a crisp pickup question and sleep:
  `maestro ask "$KEY" "Pick up $KEY — <one-line plan>. AC: <bulleted>. OK?"`

### `awaiting-human`
You only ran because an answer arrived (already folded above). Read it from the events:
`maestro events "$KEY" --since <observed_seq>`. Then:
- approved → `maestro set-phase "$KEY" ready --reason "approved: <verbatim>"`
- rejected/`discard` → `maestro set-phase "$KEY" terminating`
- modified scope → note it, then `maestro set-phase "$KEY" ready`
**Then** `maestro inbox-ack "$KEY"` (LAST — so a crash before this re-reads the answer).

### `ready`
Honor `dependsOn` in the spec: if any listed ticket isn't `done`, sleep
`maestro requeue "$KEY" 300` and exit. Otherwise create the worktree and begin:
```bash
git -C "$REPO" fetch -q origin main
git -C "$REPO" worktree add "$HOME/worktrees/$KEY" -b "${PREFIX}${KEY}" origin/main 2>/dev/null \
  || git -C "$REPO" worktree add "$HOME/worktrees/$KEY" "${PREFIX}${KEY}"   # adopt if branch exists
maestro set-phase "$KEY" implementing --reason "worktree ready"
```

### `implementing`
You are (or the dispatcher cd'd you) in `$HOME/worktrees/$KEY`. Implement the spec's
Acceptance criteria:
1. Read the spec's Intent + AC and the relevant code. Make the change.
2. **Tests are the proof.** Add/extend tests under `tests/`. Then:
   ```bash
   cd "$HOME/worktrees/$KEY" && python3 -m venv .venv 2>/dev/null; .venv/bin/pip -q install -e ".[dev]" >/dev/null 2>&1
   .venv/bin/python -m pytest -q
   ```
   If red, fix and re-run. Do not proceed until green. If you exceed ~`max_impl_turns`
   edit/test cycles without converging: `maestro fail "$KEY" "non-converging: <why>"` and exit.
3. Commit, push, open a **draft** PR, and record it idempotently:
   ```bash
   git -C "$HOME/worktrees/$KEY" add -A && git -C "$HOME/worktrees/$KEY" commit -q -m "$KEY: <subject>"
   git -C "$HOME/worktrees/$KEY" push -q -u origin "${PREFIX}${KEY}"
   PR_URL=$(gh pr create --repo cortop/maestro --head "${PREFIX}${KEY}" --draft \
            --title "$KEY: <subject>" --body "<motivation/changes/AC-with-output>" 2>/dev/null \
            || gh pr view "${PREFIX}${KEY}" --repo cortop/maestro --json url -q .url)
   PR_NUM=$(gh pr view "${PREFIX}${KEY}" --repo cortop/maestro --json number -q .number)
   maestro append "$KEY" --type PrOpened --payload "{\"number\":$PR_NUM,\"url\":\"$PR_URL\",\"draft\":true}" --step-id "pr-$KEY"
   maestro set-phase "$KEY" awaiting-ci --requeue 300
   ```
   Then exit (do NOT poll CI in-session).
   Rules: never force-push, never skip hooks, never mock real behavior in tests.

### `awaiting-ci`
You woke on the timer. Check CI once and record it:
```bash
STATE=$(gh pr checks "$(gh pr view ${PREFIX}${KEY} --repo cortop/maestro --json number -q .number)" --repo cortop/maestro 2>/dev/null \
        | awk '{print $2}' | sort -u | paste -sd, - | grep -q fail && echo failing || echo passing)
maestro append "$KEY" --type CiObserved --payload "{\"state\":\"$STATE\"}" --step-id "ci-$KEY-$(date +%s)"
```
- passing → `maestro set-phase "$KEY" in-review`
- failing → `maestro set-phase "$KEY" implementing`
- still pending → `maestro requeue "$KEY" 300`

### `in-review`
If the PR is merged (`gh pr view ... --json state`), clean up and finish:
```bash
git -C "$REPO" worktree remove "$HOME/worktrees/$KEY" --force 2>/dev/null || true
maestro finalize "$KEY"
```
Else sleep: `maestro requeue "$KEY" 900`.

### `degraded`
Do nothing automated — wait for `maestro cmd "$KEY" retry`. Exit.

---
**Exit checklist:** you appended at most one logical step, you did NOT hand-edit
`events/`/`derived/`/snapshots, and you ran `maestro release "$KEY"`.
