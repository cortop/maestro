---
description: Reconcile an `implementing` maestro ticket — code the ACs, adversarial QA, self-review, open a PR. (maestro self-dev)
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` — implementing (self-development)

You are the reconciler for ticket **`$1`** of the maestro project, spawned because it is
currently in the `implementing` phase. Take **exactly ONE** step toward its desired state,
record it only through the `maestro` CLI, then exit. The dispatcher re-spawns you next sweep,
routing to whichever phase file matches the ticket's phase at that time — this file only ever
handles `implementing`. You are a top-level session, so you may use the `Agent` tool — this is
how the Implementer↔QA loop below runs.

## Always: load state first
Resolve this ticket's bound repo and the board-wide home as literals — this preamble runs no
`eval`, `python3`, `sed`, or `cat`. REPO/SLUG/BASE/PREFIX/MODE come from `maestro env --key`, which
can differ per ticket in a multi-repo home (single-repo homes fall back to the legacy
`repo_path`/`branch_prefix` config, so this is unchanged there) — plus MHOME and
QA_STANDARDS_AXIS, which are board-wide and come from the key-less `maestro env`. `MODE` is `git`
(default — worktree/branch/PR, the rest of this doc unless said otherwise) or `local` (AD-6 — a
plain directory, e.g. a notes vault or `~/.claude` for self-editing skills, with no branch/PR
path; called out explicitly below wherever it changes what you do):
```bash
KEY="$1"
maestro env --key "$KEY"   # -> repo_path/slug/base_branch/branch_prefix/mode/reconcile_command
maestro env                # -> home/qa_standards_axis (board-wide; keyless)
maestro observe-spec "$KEY"
maestro snapshot "$KEY"                     # -> phase, pr, ci, failure_count, open_questions
```
Read the two JSON outputs above and hold their fields as literals for the rest of this file: REPO
(`repo_path`), SLUG (`slug`), BASE (`base_branch`), PREFIX (`branch_prefix`), MODE (`mode`) from
the first call; MHOME (`home`) and QA_STANDARDS_AXIS (`qa_standards_axis`) from the second. Then,
with the **Read** tool — never `cat`/`sed`, this preamble reads no file via the shell — load:
- `<MHOME>/tickets/<KEY>/spec.md` — desired state (you never edit this)
- `<MHOME>/derived/context/<KEY>.md` — folded log: verbatim Q&A, phase reasons, failures, CI
  history, recent impl steps, dependsOn phases — read this before acting, it saves re-deriving
  context from raw events. It may not exist yet for a brand-new ticket; a Read error there just
  means no context has been folded yet, not a failure.

If the snapshot shows pending inbox commands, fold them before deciding:
`maestro fold-inbox "$KEY"`. Finish every exit path with `maestro release "$KEY"` (drop your claim).

## `implementing`: code the ACs, prove them, open a PR

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
   **Done when:** `maestro local-backup` ran before the first edit, every spec AC has a
   `verify-ac` attestation, and `maestro finalize "$KEY"` has appended its event. Then exit.

**If `MODE == git`** (default): you are (or the dispatcher cd'd you) in `$MHOME/worktrees/$KEY`
(if the worktree is missing, recreate it as in the `ready` phase file — `worktree add` adopts
the existing `${PREFIX}${KEY}` branch).

**Step 0 — sync with the base branch (also how conflicts get resolved).** You may have landed
here because `check-conflicts` found the PR `CONFLICTING` (snapshot `reason` says so). Always
rebase onto the latest base first, then resolve any conflicts — always resolve, never
`git rebase --abort`:
```bash
WT="$MHOME/worktrees/$KEY"
git -C "$REPO" fetch -q origin "$BASE"
git -C "$WT" rebase "origin/$BASE" || true   # resolve conflicts, then: git -C "$WT" rebase --continue
```
Before resolving a conflicting hunk, recover the intent on **both** sides — read the commit(s)
(and, if the subject references one, the PR/ticket) that introduced the conflicting lines on
`origin/$BASE` (`git log -1 --format='%H %s' <sha>`, `gh pr view <n>` if it names a PR number),
alongside this ticket's own spec Intent — so the merge reconciles what both sides were actually
trying to do, not just a textual splice.

If a PR is already open (snapshot `pr_number` is set) and its Acceptance criteria are already
implemented, you are here **only to resolve the conflict** — resolve, run tests, then skip to
step 5 (push the rebased branch + `set-phase awaiting-ci`). Keep the prior attestations and QA
verdicts as-is (the spec, and so their content hashes, didn't change) — do not re-implement the
feature, re-run `verify-ac`, or re-run the QA loop below for this pass. If you truly cannot
reconcile the two intents yourself, escalate:
`maestro ask "$KEY" "PR #<n> conflict I couldn't auto-resolve: <detail>" --qid "conflict-$KEY-<n>"`
and exit.

Otherwise implement the spec's Acceptance criteria:
1. Read the spec's Intent + AC and the relevant code. Make the change.
2. **Tests are the proof — QA against the real app, not mocks.** Every change ships with a test
   that exercises the actual surface and shows the feature working end-to-end: drive the real
   `maestro` CLI / a real dispatcher sweep (`dispatch(cfg, DryRunSessions(), ...)`) over a temp
   home and assert the resulting events/snapshot/output. For the TUI (`tui*.py`) the proof must
   **mount the real app** — extend `tests/test_tui_runtime.py` (`async with app.run_test() as
   pilot:` + the binding sweep / `test_every_binding_action_resolves`). Mock only the external
   `claude -p` / network / `launchctl` boundary — test the real thing under review everywhere
   else. Then (install the `tui` extra too, so TUI runtime tests run instead of skipping):
   ```bash
   cd "$MHOME/worktrees/$KEY" && python3 -m venv .venv 2>/dev/null; .venv/bin/pip -q install -e ".[dev,tui]" >/dev/null 2>&1
   .venv/bin/python -m pytest -q
   ```
   If red, fix and re-run — stay on this step until green. If you exceed ~`max_impl_turns`
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
   does not block `set-phase awaiting-ci` (explicit, tested choice — see
   `ops._refuse_if_qa_failing`). Fix any cheap, clearly-right Standards findings alongside the
   Spec-axis fixes below; for the rest, leave a `maestro append --type Note` breadcrumb summarizing
   what a human reviewer should look at and proceed — do not loop or block on this axis.
   - **Every AC verdict PASS** (spec axis) → continue to step 4.
   - **Any AC verdict FAIL** → you (the implementer) fix the code per the QA evidence, record the
     turn with `maestro impl-turn "$KEY" --role implementer` (the verb numbers the turn and mints
     its own step-id itself — never hand-roll this with `maestro append`), re-run tests, then spawn
     QA again against the refreshed diff. Bound the rounds at `max_impl_turns` implementer turns —
     `impl-turn` checks the ceiling itself and routes the crossing call to `ops.fail`
     (backoff/dead-letter), so this is not just a convention you can silently overrun; if still
     failing when exhausted, do not open a PR — `maestro set-phase "$KEY" implementing --reason "QA
     loop non-converging after N rounds: <summary>"` then `maestro requeue "$KEY" 60` and exit so
     the dispatcher resumes the loop next sweep. Separately, `set-phase awaiting-ci` refuses
     (raises, no event appended) while any current AC's latest QA verdict is `fail`, so a failing
     verdict always routes back to `implementing`, never onward to `awaiting-ci`.
4. **Self-review gate — one structured attestation per spec AC, before opening the PR:**
   for each `- [ ] ...` checkbox in the spec, `maestro verify-ac "$KEY" --ac <n> --what
   "<what you ran>" --where "<file:line or test name>" --result "<the observed outcome>"`
   (1-based, in spec order; content-hash keyed, so a later spec edit to that line un-verifies
   it again — re-run verify-ac if that happens). All three fields are required — a call
   missing any of them is rejected. This is a structured self-attestation that saves the human
   reviewer time — the QA loop above is what makes it independently checked; cite the real
   evidence (a test name, a diff hunk), never rubber-stamp it. Step 5's `set-phase awaiting-ci`
   refuses with a non-zero exit and appends no event if any spec AC is still unverified, and
   also refuses while any current AC's latest QA verdict is `fail` — verify all of them here.
   (A human can still force a ticket through with `--force` on `set-phase`, which records
   `forced_by=<actor>` in the event log — that escape hatch overrides only the unverified-ACs
   gate, not a failing QA verdict, and is for a human override; if you truly cannot verify an
   AC, ask instead of reaching for it yourself.)
5. Commit, push, open a **draft** PR with an AC-to-evidence table, and record it idempotently:
   ```bash
   git -C "$MHOME/worktrees/$KEY" add -A && git -C "$MHOME/worktrees/$KEY" commit -q -m "$KEY: <subject>"
   git -C "$MHOME/worktrees/$KEY" push -q -u origin "${PREFIX}${KEY}"
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
   Push normally — never force-push. Let hooks run — never skip them. Test the real behavior,
   never a mock. Then exit; the dispatcher's next sweep polls CI, this session does not.

**Done when** either: (a) tests are green, every spec AC has a passing QA verdict and a
`verify-ac` attestation, a PR is open with `PrOpened` recorded, and `set-phase awaiting-ci` has
appended (that call itself refuses unless the gate above actually passed, so reaching it is
proof); (b) this was a conflict-only pass — the rebase is clean (or escalated via `maestro ask`
with a `conflict-$KEY-<n>` qid), tests are green, and the branch is pushed with `set-phase
awaiting-ci` appended; or (c) the QA loop or tests did not converge within `max_impl_turns` and
you appended `maestro fail` or `set-phase implementing --reason "QA loop non-converging..."` +
`maestro requeue "$KEY" 60`. In every case, `maestro release "$KEY"` has run.
