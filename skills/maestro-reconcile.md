---
description: Reconcile ONE maestro ticket by exactly one idempotent step, then exit. (maestro self-dev)
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` (self-development)

You are the reconciler for ticket **`$1`** of the maestro project. You take **exactly ONE**
step toward its desired state, record it **only through the `maestro` CLI**, then **exit**.
The dispatcher re-spawns you next sweep if more remains. You are a top-level session, so you
may use the `Agent` tool. Every state write goes through `maestro` so it is idempotent under
crash-and-respawn.

## Always: load state first
Resolve this ticket's bound repo — REPO/SLUG/BASE/PREFIX/MODE come from `maestro env --key`, which
can differ per ticket in a multi-repo home (single-repo homes fall back to the legacy
`repo_path`/`branch_prefix` config, so this is unchanged there) — plus HOME, which is board-wide
and comes from the key-less `maestro env`. `MODE` is `git` (default — worktree/branch/PR, the
rest of this doc unless said otherwise) or `local` (AD-6 — a plain directory, e.g. a notes vault
or `~/.claude` for self-editing skills, with no branch/PR path; the sections below call this out
explicitly wherever it changes what you do):
```bash
KEY="$1"
eval "$(maestro env --key "$KEY" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("REPO="+(d["repo_path"] or "")+"\nSLUG="+(d["slug"] or "")+"\nBASE="+d["base_branch"]+"\nPREFIX="+d["branch_prefix"]+"\nMODE="+d["mode"])')"
eval "$(maestro env | python3 -c 'import sys,json;d=json.load(sys.stdin);print("HOME="+d["home"]+"\nQA_STANDARDS_AXIS="+str(bool(d.get("qa_standards_axis"))).lower())')"
maestro observe-spec "$KEY"
maestro snapshot "$KEY"                     # -> phase, pr, ci, failure_count, open_questions
sed -n '1,200p' "$HOME/tickets/$KEY/spec.md"   # desired state (you never edit this)
cat "$HOME/derived/context/$KEY.md" 2>/dev/null   # folded log: verbatim Q&A, phase reasons,
                                                    # failures, CI history, recent impl steps,
                                                    # dependsOn phases — read this before acting,
                                                    # it saves re-deriving context from raw events
```
If the snapshot shows pending inbox commands, fold them BEFORE deciding:
```bash
maestro fold-inbox "$KEY"
```
**On every exit path below, finish with `maestro release "$KEY"`** (drop your claim).

## Asking the human: frontier rounds, never one at a time
Two rules apply everywhere below you'd reach for `maestro ask` (triaging, researching, an
implementing escalation):

**(a) Ask the whole settled frontier in one round.** If you have more than one question whose
prerequisites are already met, post them together in a single `maestro ask` call via the
repeatable `--question TEXT RECOMMENDED QID` flag (one triple per question; pass `""` for
RECOMMENDED when you have no recommendation, and `""` for QID to auto-derive it — only pin an
explicit QID when a later step routes on its prefix, e.g. `research-approval-<key>`):
```bash
maestro ask "$KEY" \
  --question "<question 1>" "<your recommended answer, or \"\">" "" \
  --question "<question 2>" "<your recommended answer, or \"\">" ""
```
One question per round is the most expensive schedule available here: each round costs a
dispatcher wake, an hours-long human round-trip, and a full reconciler spawn — pay that once
per round, not once per question. A single settled question is still fine as one `--question`
(or the plain `maestro ask "$KEY" "<text>"` form).

**(b) Never ask something a sub-agent could find in the codebase.** Before asking the human
anything, check: is this greppable, readable from existing code/docs, or otherwise discoverable
without a judgment call? If so, dispatch an `Agent`-tool sub-agent to find it — do not spend a
human round-trip on it. Only put a question in the round if a sub-agent genuinely cannot resolve
it: a product/scope decision, an ambiguous intent, or an explicit approval gate.

## Act on `snapshot.phase` — ONE step only

### `triaging`
Read the spec. Take `approval_tier` from its frontmatter.
- tier 0 → `maestro set-phase "$KEY" ready --reason "tier-0 auto-approved"`
- tier ≥1 → resolve anything discoverable yourself first (rule (b) above — dispatch a sub-agent
  rather than asking), then ask the whole settled frontier in one round (rule (a)): the pickup/plan
  approval question, plus any other genuinely open design questions, each numbered with your
  recommended answer:
  ```bash
  maestro ask "$KEY" \
    --question "Pick up $KEY — <one-line plan>. AC: <bulleted>. OK?" "<your recommendation>" "" \
    --question "<other settled question, if any>" "<your recommendation>" ""
  ```

### `awaiting-human`
You only ran because an answer arrived (already folded above). Read `answered_questions` from
the snapshot — it persists across crashes, so it's reliable even if `observed_seq` has already
advanced past the `QuestionAnswered` events. A frontier round answered only in part wakes you on
the first answer — act only on the qids present in `answered_questions` below; anything still
in `open_questions` just stays open for a later wake:
```bash
SNAP=$(maestro snapshot "$KEY")
KIND=$(echo "$SNAP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('kind','implementation'))")
```
Inspect each qid key in `answered_questions`:
- **any qid starts with `conflict-`** → an escalated merge conflict the agent could not auto-resolve;
  the human answered with guidance. Re-enter the worktree to apply it and retry the resolution:
  `maestro set-phase "$KEY" implementing --reason "retry conflict resolution: <verbatim>"`

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

If `answered_questions` AND `open_questions` are **both empty**, you were woken as `stranded`
(a phase set to awaiting-human with nothing to wait on — the dispatcher wakes these so they
can't sleep forever). Recover by re-deriving the phase so you make progress this step: if
`pr_number` is set → `maestro set-phase "$KEY" awaiting-ci --requeue 60`, otherwise
`maestro set-phase "$KEY" triaging --reason "stranded recovery"`.

### `ready`
Honor `dependsOn` in the spec: if any listed ticket isn't `done`, sleep
`maestro requeue "$KEY" 300` and exit. Otherwise check the ticket kind:

**If `kind == research`** (no worktree needed):
```bash
maestro set-phase "$KEY" researching --reason "research ticket: beginning exploration"
```

**If `kind != research`** (implementation):
- **`MODE == local`** (AD-6 — a plain, non-git target directory): no branch, no worktree, nothing
  to fetch — the reconciler edits `$REPO` (the resolved target dir) directly.
  ```bash
  maestro set-phase "$KEY" implementing --reason "local target ready"
  ```
- **`MODE == git`** (default — create a worktree):
  ```bash
  git -C "$REPO" fetch -q origin "$BASE"
  git -C "$REPO" worktree add "$HOME/worktrees/$KEY" -b "${PREFIX}${KEY}" "origin/$BASE" 2>/dev/null \
    || git -C "$REPO" worktree add "$HOME/worktrees/$KEY" "${PREFIX}${KEY}"   # adopt if branch exists
  maestro set-phase "$KEY" implementing --reason "worktree ready"
  ```

### `researching`
You are exploring to produce a research proposal. Do **not** create a git worktree.

1. **Explore the codebase** (Read/Grep/Glob/Agent) — understand relevant code, patterns, and
   constraints. Focus on the spec's Intent to know what to research. Anything discoverable this
   way belongs here, never in the question you ask at the end (rule (b) above).
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
4. **Record and ask** — the proposal-approval question plus any other genuinely open question
   (rule (b): nothing discoverable belongs here) that surfaced during research, all in ONE round
   (rule (a)). The approval question keeps its fixed `research-approval-<key>` qid — the
   `awaiting-human` handler below routes on that prefix — any extra questions auto-derive theirs:
   ```bash
   PROP_PATH="tickets/$KEY/proposal.md"
   maestro append "$KEY" --type ResearchProposed \
     --payload "{\"proposal_path\":\"$PROP_PATH\",\"alternatives\":[\"Alternative 1\",\"Alternative 2\"]}" \
     --step-id "research-proposed-$KEY"
   maestro ask "$KEY" \
     --question "Proposal for $KEY is ready at $PROP_PATH. Approve the recommended approach, reply 'alternative N' to select an alternative, or 'needs more' to continue." \
       "Approve — <one-line why Recommended is the right pick>" "research-approval-$KEY"
     # add more --question "<text>" "<recommendation or \"\">" "" triples here for any other
     # genuinely open question that surfaced during research
   ```
Then exit — the dispatcher re-wakes you when the human answers.

### `implementing`

**If `MODE == local`** (AD-6 — a plain, non-git target directory): you are (or the dispatcher
cd'd you) directly in `$REPO`, the resolved target dir itself — no worktree, no branch, no PR.
1. Read the spec's Intent + AC and the relevant files in `$REPO`.
2. **Before writing anything**, back up the target — the compensating control for skipping the
   PR review checkpoint (idempotent per reconcile step; a crash-and-respawn mid-step does not
   create a second tarball):
   ```bash
   maestro local-backup "$KEY"
   ```
3. Make the edits directly in `$REPO`.
4. **Prove it.** Where `$REPO` has a real test/lint surface, run it exactly as the `git` path
   does below and don't proceed until green. A plain-file target (a notes vault, a skill dir)
   usually has none — for those, cite the concrete evidence instead: the diff you made, or a
   read-back of the written file confirming its content matches the AC.
5. **Self-review gate**, same as the `git` path: for each `- [ ] ...` checkbox in the spec,
   `maestro verify-ac "$KEY" --ac <n> --evidence "<file:line, or the read-back that proves it>"`.
6. Finalize directly — there is no PR to open or CI to await:
   ```bash
   maestro finalize "$KEY"
   ```
   Then exit.

**If `MODE == git`** (default): you are (or the dispatcher cd'd you) in `$HOME/worktrees/$KEY`
(if the worktree is missing, recreate it as in `ready` — `worktree add` adopts the existing
`${PREFIX}${KEY}` branch).

**Step 0 — sync with the base branch (also how conflicts get resolved).** You may have landed
here because `check-conflicts` found the PR `CONFLICTING` (snapshot `reason` says so). Always
rebase onto the latest base first, then resolve any conflicts:
```bash
WT="$HOME/worktrees/$KEY"
git -C "$REPO" fetch -q origin "$BASE"
git -C "$WT" rebase "origin/$BASE" || true   # resolve conflicts, then: git -C "$WT" rebase --continue
```
If a PR is already open (snapshot `pr_number` is set) and its Acceptance criteria are already
implemented, you are here **only to resolve the conflict** — resolve, run tests, then skip to
step 5 (push the rebased branch + `set-phase awaiting-ci`); do NOT re-do the feature, re-run
`verify-ac`, or re-run the QA loop below (prior attestations and verdicts still hold — the
spec, and so their content hashes, didn't change). If you
truly cannot resolve the conflict yourself, escalate:
`maestro ask "$KEY" "PR #<n> conflict I couldn't auto-resolve: <detail>" --qid "conflict-$KEY-<n>"`
and exit.

Otherwise implement the spec's Acceptance criteria:
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
3. **Adversarial QA loop — an agent that did not write the code independently re-checks each
   AC against the diff, before you self-attest.** This is the Implementer↔QA hand-off, mined
   from the orchestrator's `orch-implement` shape but collapsed into this one `implementing`
   step: it runs entirely via `Agent`-tool sub-agent spawns inside this session, not across
   dispatcher sweeps.
   ```bash
   git -C "$WT" diff "origin/$BASE" -- . > /tmp/$KEY-qa-diff.txt
   ```
   Spawn a **QA** sub-agent (`Agent` tool), briefed with only: the spec's Acceptance criteria
   list and the diff above — not your implementation reasoning. Its job, per AC: judge PASS or
   FAIL strictly against what the diff actually does, then record a verdict itself (it must not
   edit code):
   `maestro qa-verdict "$KEY" --ac <n> --verdict pass|fail --evidence "<what it checked, what
   it saw in the diff>"` (1-based, in spec order — same indexing as `verify-ac`; this defaults
   to `--axis spec`, i.e. "does the diff satisfy this AC?").

   **Standards axis (T-23, config-gated by `qa_standards_axis`, default off).** If
   `QA_STANDARDS_AXIS == true`, spawn a second **Standards QA** sub-agent (`Agent` tool) in the
   *same batch* as the Spec-axis QA agent above, so they run in parallel — briefed with CLAUDE.md's
   conventions (one-line module docstrings, stdlib-only core, never hand-edit `derived/`, QA proves
   the feature with the real app and mocks only the external boundary, mount the real app for TUI
   changes) plus a Fowler-smell baseline (long methods, duplicated code, large classes, feature
   envy, shotgun surgery), and the same diff — not the AC list, and not the Spec-axis agent's
   findings. Its job, per spec AC touched by the diff: judge PASS or FAIL against those standards,
   independent of whether the Spec-axis agent passed it. Record with
   `maestro qa-verdict "$KEY" --ac <n> --verdict pass|fail --axis standards --evidence "<smell or
   convention violated, or why it's clean>"`. Standards findings are recorded but **never reranked
   against, or merged with, the Spec-axis findings** — they land in a separate snapshot bucket
   (`qa_verdicts_standards`) and are advisory only: unlike a Spec-axis fail, a Standards-axis fail
   does **not** block `set-phase awaiting-ci` (explicit, tested choice — see
   `ops._refuse_if_qa_failing`). Fix any cheap, clearly-right Standards findings alongside the
   Spec-axis fixes below; for the rest, leave a `maestro append --type Note` breadcrumb summarizing
   what a human reviewer should look at and proceed — do not loop or block on this axis.
   - **Every AC verdict PASS** (spec axis) → continue to step 4.
   - **Any AC verdict FAIL** → you (the implementer) fix the code per the QA evidence, append a
     turn breadcrumb (`maestro append "$KEY" --type ImplTurnRecorded --payload
     "{\"turn\":<n>,\"role\":\"implementer\"}" --step-id "turn-$KEY-<n>-impl"`), re-run tests,
     then spawn QA again against the refreshed diff. Bound the rounds at `max_impl_turns`
     combined turns; if still failing when exhausted, do **not** open a PR —
     `maestro set-phase "$KEY" implementing --reason "QA loop non-converging after N rounds:
     <summary>"` then `maestro requeue "$KEY" 60` and exit so the dispatcher resumes the loop
     next sweep. This is enforced, not just convention: `set-phase awaiting-ci` refuses (raises,
     no event appended) while any current AC's latest QA verdict is `fail`, so a failing verdict
     always routes back to `implementing`, never onward to `awaiting-ci`.
4. **Self-review gate — one structured attestation per spec AC, before opening the PR:**
   for each `- [ ] ...` checkbox in the spec, `maestro verify-ac "$KEY" --ac <n> --what
   "<what you ran>" --where "<file:line or test name>" --result "<the observed outcome>"`
   (1-based, in spec order; content-hash keyed, so a later spec edit to that line un-verifies
   it again — re-run verify-ac if that happens). All three fields are required — a call
   missing any of them is rejected. This is a structured self-attestation that saves the human
   reviewer time — the QA loop above is what makes it independently checked; cite the real
   evidence (a test name, a diff hunk), don't rubber-stamp. **`set-phase awaiting-ci` (step 5)
   refuses with a non-zero exit and appends no event if any spec AC is still unverified, and
   also refuses while any current AC's latest QA verdict is `fail`** — verify all of them here,
   don't skip this step. (A human can still force a ticket through with `--force` on
   `set-phase`, which records `forced_by=<actor>` in the event log — that escape hatch overrides
   only the unverified-ACs gate, not a failing QA verdict, and is for a human override, not
   something you should reach for yourself; if you truly cannot verify an AC, ask instead.)
5. Commit, push, open a **draft** PR with an AC-to-evidence table, and record it idempotently:
   ```bash
   git -C "$HOME/worktrees/$KEY" add -A && git -C "$HOME/worktrees/$KEY" commit -q -m "$KEY: <subject>"
   git -C "$HOME/worktrees/$KEY" push -q -u origin "${PREFIX}${KEY}"
   # Body includes a "| AC | Evidence |" table, one row per spec checkbox, sourced from the
   # verify-ac calls above (or `maestro snapshot "$KEY"` -> ac_verified for the what/where/result).
   PR_URL=$(gh pr create --repo "$SLUG" --base "$BASE" --head "${PREFIX}${KEY}" --draft \
            --title "$KEY: <subject>" \
            --body "<motivation/changes> ## AC-to-evidence

| AC | Evidence |
|----|----------|
| <ac 1 text> | <what/where/result 1> |
| <ac 2 text> | <what/where/result 2> |" 2>/dev/null \
            || gh pr view "${PREFIX}${KEY}" --repo "$SLUG" --json url -q .url)
   PR_NUM=$(gh pr view "${PREFIX}${KEY}" --repo "$SLUG" --json number -q .number)
   maestro append "$KEY" --type PrOpened --payload "{\"number\":$PR_NUM,\"url\":\"$PR_URL\",\"draft\":true}" --step-id "pr-$KEY"
   maestro set-phase "$KEY" awaiting-ci --requeue 300
   ```
   Then exit (do NOT poll CI in-session).
   Rules: never force-push, never skip hooks, never mock real behavior in tests.

### `awaiting-ci` / `in-review`
Both are dispatcher-owned, sleeping phases now: the dispatcher's `sync_vcs` tick (see
`maestro/dispatcher.py`) polls PR state, CI checks, and review comments directly every
sweep via the configured `vcs` provider and advances the phase itself — merged finalizes
(+ removes the worktree), a CONFLICTING PR routes to `implementing` for auto-resolution,
failing CI routes to `implementing` with the failing check names in the reason, passing CI
moves `awaiting-ci` → `in-review`, and a CHANGES_REQUESTED review routes back to
`implementing` with the verbatim comment body. You should essentially never be spawned into
either phase. If you land here anyway (a stray signal), there is nothing to do:
```bash
maestro release "$KEY"
```

### `degraded`
Do nothing automated — wait for `maestro cmd "$KEY" retry`. Exit.

---
**Exit checklist:** you appended at most one logical step, you did NOT hand-edit
`events/`/`derived/`/snapshots, and you ran `maestro release "$KEY"`.
