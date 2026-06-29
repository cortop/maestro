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
You only ran because an answer arrived (already folded above). Read `answered_questions` from
the snapshot — it persists across crashes, so it's reliable even if `observed_seq` has already
advanced past the `QuestionAnswered` events:
```bash
SNAP=$(maestro snapshot "$KEY")
KIND=$(echo "$SNAP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('kind','implementation'))")
```
Inspect each qid key in `answered_questions`:
- **any qid starts with `conflict-`** → the human resolved a merge conflict; go back to check CI:
  `maestro set-phase "$KEY" awaiting-ci --requeue 60`

**If `KIND == research`** (research approval question — qid starts with `research-approval-`):
Read the proposal at `$HOME/tickets/$KEY/proposal.md`. Inspect the answer:
- **"needs more"** → go back to researching:
  `maestro set-phase "$KEY" researching --reason "needs more research per human"`
- **"alternative N"** (e.g. "alternative 2") → extract the Nth alternative's section from proposal.md
- **any other approval** (yes/ok/approve/recommended) → use the `## Recommended` section from proposal.md

Then mint the implementation ticket using the chosen approach as intent:
```bash
maestro create --tier 0 --kind implementation \
  --title "Implement: <research-title-without-Research-prefix>" \
  --intent "<chosen approach text>" \
  --notes "Seeded from $KEY proposal. See tickets/$KEY/proposal.md for full context." \
  --depends-on "$KEY" --no-nudge
```
The new ticket key is auto-assigned (T-N). Append a breadcrumb linking the two, then finalize:
```bash
# Derive the impl key: last entry in the _new inbox
IMPL_KEY=$(python3 -c "
import json, pathlib
home = pathlib.Path('$HOME')
entries = [json.loads(l) for l in (home/'inbox/_new.jsonl').read_text().splitlines() if l.strip()]
print(entries[-1].get('key') or 'unknown')
" 2>/dev/null || echo "unknown")
maestro append "$KEY" --type Note \
  --payload "{\"text\":\"Created implementation ticket $IMPL_KEY from approved proposal\"}" \
  --step-id "note-impl-created-$KEY"
maestro finalize "$KEY"
```
**Then** `maestro inbox-ack "$KEY"` (LAST — so a crash before this re-reads the answer).

**If `KIND != research`** (standard implementation ticket):
- **approved** (answer is affirmative and qid is not a conflict-) → `maestro set-phase "$KEY" ready --reason "approved: <verbatim>"`
- **rejected/`discard`** → `maestro set-phase "$KEY" terminating`
- **modified scope** → note it, then `maestro set-phase "$KEY" ready`
**Then** `maestro inbox-ack "$KEY"` (LAST — so a crash before this re-reads the answer).

### `ready`
Honor `dependsOn` in the spec: if any listed ticket isn't `done`, sleep
`maestro requeue "$KEY" 300` and exit. Otherwise check the ticket kind:

**If `kind == research`** (no worktree needed):
```bash
maestro set-phase "$KEY" researching --reason "research ticket: beginning exploration"
```

**If `kind != research`** (implementation — create worktree):
```bash
git -C "$REPO" fetch -q origin main
git -C "$REPO" worktree add "$HOME/worktrees/$KEY" -b "${PREFIX}${KEY}" origin/main 2>/dev/null \
  || git -C "$REPO" worktree add "$HOME/worktrees/$KEY" "${PREFIX}${KEY}"   # adopt if branch exists
maestro set-phase "$KEY" implementing --reason "worktree ready"
```

### `researching`
You are exploring to produce a research proposal. Do **not** create a git worktree.

1. **Explore the codebase** (Read/Grep/Glob/Agent) — understand relevant code, patterns, and
   constraints. Focus on the spec's Intent to know what to research.
2. **Search the web** — use WebSearch/WebFetch or the `/deep-research` skill to find
   state-of-the-art approaches, libraries, prior art, and relevant citations.
   (If web tools are unavailable in this session, note that and fall back to codebase-only.)
3. **Write the proposal** at `$HOME/tickets/$KEY/proposal.md`:
   ```markdown
   # Proposal: <title>

   ## Recommended
   <concise description of the best approach and rationale>

   ## Alternative 1
   <description of first alternative>

   ## Alternative 2
   <description of second alternative (add more as needed)>

   ## Sources
   - <file:line> — <why relevant>
   - <https://url> — <why relevant>
   ```
4. **Record and ask**:
   ```bash
   PROP_PATH="tickets/$KEY/proposal.md"
   maestro append "$KEY" --type ResearchProposed \
     --payload "{\"proposal_path\":\"$PROP_PATH\",\"alternatives\":[\"Alternative 1\",\"Alternative 2\"]}" \
     --step-id "research-proposed-$KEY"
   maestro ask "$KEY" \
     "Proposal for $KEY is ready at $PROP_PATH. Approve the recommended approach, reply 'alternative N' to select an alternative, or 'needs more' to continue." \
     --qid "research-approval-$KEY"
   ```
Then exit — the dispatcher re-wakes you when the human answers.

### `implementing`
You are (or the dispatcher cd'd you) in `$HOME/worktrees/$KEY`. Implement the spec's
Acceptance criteria:
1. Read the spec's Intent + AC and the relevant code. Make the change.
2. **Tests are the proof — QA against the real app, not mocks.** Every change ships with a test
   that exercises the actual surface and shows the feature working end-to-end: drive the real
   `maestro` CLI / a real dispatcher sweep (`dispatch(cfg, DryRunSessions(), ...)`) over a temp
   home and assert the resulting events/snapshot/output. For the TUI (`tui*.py`) the proof must
   **mount the real app** — extend `tests/test_tui_runtime.py` (`async with app.run_test() as
   pilot:` + the binding sweep / `test_every_binding_action_resolves`). Mock ONLY the external
   `claude -p` / network / `launchctl` boundary, never the thing under test. Then (install the
   `tui` extra too, so TUI runtime tests run instead of skipping):
   ```bash
   cd "$HOME/worktrees/$KEY" && python3 -m venv .venv 2>/dev/null; .venv/bin/pip -q install -e ".[dev,tui]" >/dev/null 2>&1
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
You woke on the timer. **First** check for merge conflicts (idempotent — skips if question already open):
```bash
PR_NUM=$(maestro snapshot "$KEY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('pr_number') or '')")
if [ -n "$PR_NUM" ]; then
  MERGEABLE=$(gh pr view "$PR_NUM" --repo cortop/maestro --json mergeable -q .mergeable 2>/dev/null || echo "UNKNOWN")
  maestro check-conflicts "$KEY" "$PR_NUM" "$MERGEABLE"
  # If CONFLICTING, check-conflicts transitions to awaiting-human — exit now.
  [ "$MERGEABLE" = "CONFLICTING" ] && maestro release "$KEY" && exit 0
fi
```
Then check CI once and record it:
```bash
STATE=$(gh pr checks "$(gh pr view ${PREFIX}${KEY} --repo cortop/maestro --json number -q .number)" --repo cortop/maestro 2>/dev/null \
        | awk '{print $2}' | sort -u | paste -sd, - | grep -q fail && echo failing || echo passing)
maestro append "$KEY" --type CiObserved --payload "{\"state\":\"$STATE\"}" --step-id "ci-$KEY-$(date +%s)"
```
- passing → `maestro set-phase "$KEY" in-review`
- failing → `maestro set-phase "$KEY" implementing`
- still pending → `maestro requeue "$KEY" 300`

### `in-review`
**First** check for merge conflicts (guard against conflicts introduced during review):
```bash
PR_NUM=$(maestro snapshot "$KEY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('pr_number') or '')")
if [ -n "$PR_NUM" ]; then
  MERGEABLE=$(gh pr view "$PR_NUM" --repo cortop/maestro --json mergeable -q .mergeable 2>/dev/null || echo "UNKNOWN")
  maestro check-conflicts "$KEY" "$PR_NUM" "$MERGEABLE"
  [ "$MERGEABLE" = "CONFLICTING" ] && maestro release "$KEY" && exit 0
fi
```
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
