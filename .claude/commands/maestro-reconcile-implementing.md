---
description: Reconcile an `implementing` maestro ticket — code the ACs, self-review, open a PR, hand off to independent QA. (maestro self-dev)
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` — implementing (self-development)

You are the reconciler for ticket **`$1`** of the maestro project, spawned because it is
currently in the `implementing` phase. Take **exactly ONE** step toward its desired state,
record it only through the `maestro` CLI, then exit. The dispatcher re-spawns you next sweep,
routing to whichever phase file matches the ticket's phase at that time — this file only ever
handles `implementing`. QA is a separate, independent phase (`qa`) the dispatcher spawns on its
own next sweep once you hand off below — not an `Agent`-tool sub-agent you spawn here.

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
  means no context has been folded yet, not a failure. **This is also how you tell a fix round
  from a fresh implementation** — if the most recent phase-history line reads `qa -> implementing`
  with a reason citing a failing AC, `qa` sent you back; see step 1 below.

If the snapshot shows pending inbox commands, fold them before deciding:
`maestro fold-inbox "$KEY"`. Finish every exit path with `maestro release "$KEY"` (drop your claim).

## `implementing`: code the ACs, prove them, open a PR

**If `MODE == local`** (AD-6 — a plain, non-git target directory): you are (or the dispatcher
cd'd you) directly in `<REPO>`, the resolved target dir itself — no worktree, no branch, no PR.
1. Read the spec's Intent + AC and the relevant files in `<REPO>`.
2. **Before writing anything**, back up the target — the compensating control for skipping the
   PR review checkpoint (idempotent per reconcile step; a crash-and-respawn mid-step does not
   create a second tarball):
   ```bash
   maestro local-backup "$KEY"
   ```
3. Make the edits directly in `<REPO>`.
4. **Prove it.** Where `<REPO>` has a real test/lint surface, run it exactly as the `git` path
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

**If `MODE == git`** (default): you are (or the dispatcher cd'd you) in `<MHOME>/worktrees/<KEY>`
— call this directory **`<WT>`** for the rest of this file (if the worktree is missing, recreate
it exactly as the `ready` phase file does — `maestro worktree ensure "$KEY"` idempotently
creates it, or adopts the existing `<PREFIX>$KEY` branch).
`<REPO>`, `<BASE>`, `<PREFIX>`, `<SLUG>` and `<WT>` are, exactly like `<MHOME>`/`<KEY>` above,
literal values you already hold from the preamble's two `maestro env` calls — every command below
substitutes them directly when you type it; none is a shell variable a fenced line expands.

**Step 0 — sync with the base branch (also how conflicts get resolved).** You may have landed
here because `check-conflicts` found the PR `CONFLICTING` (snapshot `reason` says so), or because
a drifted-behind-base worktree was auto-rerouted here (snapshot `reason` says
`origin/<BASE> advanced (policy=...)`). If a PR is already open (snapshot `pr_number` is set),
first fetch and incorporate `origin/<PREFIX>$KEY` — the PR branch as it actually stands on
GitHub — **before** rebasing onto base. Rebasing from only the local worktree tip would silently
drop any commit pushed to the PR branch by someone else since your last sync (a CI bot's
auto-fix, a human's own push, a co-author) the moment you force-push the rebased result:
```bash
git -C <WT> fetch -q origin "<PREFIX>$KEY"
git -C <WT> merge -q --ff-only "origin/<PREFIX>$KEY"   # no-op if nothing new is there
```
If that `merge --ff-only` fails (your local tip and `origin/<PREFIX>$KEY` have diverged — someone
else pushed while you were working), reconcile properly instead of discarding either side:
`git -C <WT> merge -q "origin/<PREFIX>$KEY"` and resolve any conflict the same way step 0's own
rebase conflicts are resolved below, before continuing.

Then always rebase onto the latest base, and resolve any conflicts — always resolve, never
`git rebase --abort`:
```bash
git -C <REPO> fetch -q origin "<BASE>"
git -C <WT> rebase "origin/<BASE>"
```
A conflicting rebase exits non-zero and leaves conflict markers in the tree — resolve them, then
continue with `git -C <WT> rebase --continue` (its own, single invocation; never
`git rebase --abort`).

Before resolving a conflicting hunk, recover the intent on **both** sides — read the commit(s)
(and, if the subject references one, the PR/ticket) that introduced the conflicting lines on
`origin/<BASE>` (`git log -1 --format='%H %s' <sha>`, `gh pr view <n>` if it names a PR number),
alongside this ticket's own spec Intent — so the merge reconciles what both sides were actually
trying to do, not just a textual splice.

If a PR is already open (snapshot `pr_number` is set) and its Acceptance criteria are already
implemented, you are here **only to resolve the conflict** — resolve, run tests, then skip to
step 5 (push the rebased branch + `set-phase awaiting-ci`). Keep the prior attestations and QA
verdicts as-is (the spec, and so their content hashes, didn't change) — do not re-implement the
feature, re-run `verify-ac`, or route the ticket through `qa` for this pass. If you truly cannot
reconcile the two intents yourself, escalate:
`maestro ask "$KEY" "PR #<n> conflict I couldn't auto-resolve: <detail>" --qid "conflict-$KEY-<n>"`
and exit.

Otherwise implement the spec's Acceptance criteria:
1. Read the spec's Intent + AC and the relevant code.
   **If `qa` sent you back here** (the context file's phase history shows the most recent
   transition is `qa -> implementing`, citing a failing AC + evidence — cross-check
   `maestro snapshot "$KEY"` -> `qa_verdicts` for the same ac_hash if you want the raw record):
   this is a **fix round**, not a fresh implementation. Fix the code per that evidence and
   continue at step 2 — do not re-derive the diff or re-judge the AC yourself, that is `qa`'s own
   independent job, running in its own phase; yours here is only to fix and hand off again (step
   5). Otherwise, make the change fresh.
2. **Tests are the proof — QA against the real app, not mocks.** Every change ships with a test
   that exercises the actual surface and shows the feature working end-to-end: drive the real
   `maestro` CLI / a real dispatcher sweep (`dispatch(cfg, DryRunSessions(), ...)`) over a temp
   home and assert the resulting events/snapshot/output. For the TUI (`tui*.py`) the proof must
   **mount the real app** — extend `tests/test_tui_runtime.py` (`async with app.run_test() as
   pilot:` + the binding sweep / `test_every_binding_action_resolves`). Mock only the external
   `claude -p` / network / `launchctl` boundary — test the real thing under review everywhere
   else. The Bash tool's cwd is already `<WT>` (the dispatcher spawns this session there), so the
   test invocation below needs no `cd`. Its dependency tree (including the `tui` extra, so TUI
   runtime tests run instead of skipping) is already installed — `maestro worktree ensure` (GA-20,
   the `ready` phase file) ran the repo's declared `prime` once when this worktree was first
   created, so this step only runs the tests:
   ```bash
   .venv/bin/python -m pytest -q
   ```
   **Pytest's permission story, decided:** `.venv/bin/python` stays cwd-anchored above, never
   absolutized to an absolute path rooted at `<WT>` — `.claude/settings.json` grants only the
   *relative* prefix `Bash(.venv/bin/:*)`, which matches the command string solely while the
   shell's cwd is the worktree, and `dispatcher._worker_cwd`
   (`maestro/dispatcher.py:1369`) already runs this session with `<WT>` as cwd — so the relative
   form is both correct and the only one that avoids a permission prompt. Do not "fix" this back
   to an absolute path.
   If red, fix and re-run — stay on this step until green. If you exceed ~`max_impl_turns`
   edit/test cycles without converging: `maestro fail "$KEY" "non-converging: <why>"` and exit.
3. **If step 1 was a fix round, record it now** — the counterpart to the QA fail that sent you
   back, and the thing that bounds the implementing↔qa ping-pong:
   ```bash
   maestro impl-turn "$KEY" --role implementer
   ```
   This verb numbers the turn and mints its own step-id itself — never hand-roll this with
   `maestro append`, and never invent a second counter. It also checks `cfg.max_impl_turns` on its
   own and routes a crossing call straight to `ops.fail` (backoff/dead-letter) — if its response
   shows the ticket was parked, `maestro fail` has already run; stop here, do not push or hand off
   again, and exit. Skip this step entirely on a fresh (non-fix-round) pass.
4. **Self-review gate — one structured attestation per spec AC, before opening the PR:**
   for each `- [ ] ...` checkbox in the spec, `maestro verify-ac "$KEY" --ac <n> --what
   "<what you ran>" --where "<file:line or test name>" --result "<the observed outcome>"`
   (1-based, in spec order; content-hash keyed, so a later spec edit to that line un-verifies
   it again — re-run verify-ac if that happens; also idempotent on a fix round where the AC's
   text hasn't changed, so it's safe to call every pass). All three fields are required — a call
   missing any of them is rejected. This is a structured self-attestation that saves the human
   reviewer time — the independent `qa` phase below is what makes it independently checked; cite
   the real evidence (a test name, a diff hunk), never rubber-stamp it. The enforced gates
   (unverified ACs, a failing spec-axis QA verdict) live on `qa`'s own `set-phase awaiting-ci`
   call, in its phase file — verify every AC here so that gate passes cleanly there.
5. Commit, push, open a **draft** PR with an AC-to-evidence table, and record it idempotently —
   or, on a fix round, just push the fix (the block below already no-ops the PR creation once one
   exists). The body's table is sourced from the `verify-ac` calls above (or `maestro snapshot
   "$KEY"` -> `ac_verified` for the what/where/result):
   ```bash
   git -C <WT> add -A
   git -C <WT> commit -q -m "$KEY: <subject>"
   git -C <WT> push -q -u origin "<PREFIX>$KEY"
   gh pr create --repo "<SLUG>" --base "<BASE>" --head "<PREFIX>$KEY" --draft --title "$KEY: <subject>" --body "<motivation/changes> ## AC-to-evidence

| AC | Evidence |
|----|----------|
| <ac 1 text> | <what/where/result 1> |
| <ac 2 text> | <what/where/result 2> |"
   ```
   If that fails because a PR already exists for this branch (`gh` says so), the PR is already
   open — fetch its URL instead of retrying the create:
   ```bash
   gh pr view "<PREFIX>$KEY" --repo "<SLUG>" --json url -q .url
   ```
   Either way, read the PR number next:
   ```bash
   gh pr view "<PREFIX>$KEY" --repo "<SLUG>" --json number -q .number
   ```
   Neither the URL nor the number is captured into a shell variable — type the values you just
   read directly into the payload (`<pr-number>`/`<pr-url>` below are exactly that, not a token
   resolved from `maestro env --key`). Skip the `PrOpened` append on a fix round (a PR already
   open already has one):
   ```bash
   maestro append "$KEY" --type PrOpened --payload "{\"number\":<pr-number>,\"url\":\"<pr-url>\",\"draft\":true}" --step-id "pr-$KEY"
   maestro set-phase "$KEY" qa --requeue 300
   ```
   Push normally — never force-push. Let hooks run — never skip them. Test the real behavior,
   never a mock. Then exit; the dispatcher's next sweep spawns the independent `qa` reconciler
   (`skills/maestro-reconcile-qa.md`) — this session never judges its own diff, and does not poll
   CI itself.

**Done when** either: (a) a fresh implementation — tests are green, every spec AC has a
`verify-ac` attestation, a PR is open with `PrOpened` recorded, and `set-phase qa` has appended,
handing review off to the independent `qa` phase; (b) a fix round — the fix is made per `qa`'s
evidence, tests are green, `impl-turn` recorded the round (and did not park the ticket), the fix
is pushed, and `set-phase qa` has appended again to re-request review; (c) a conflict-only pass —
the rebase is clean (or escalated via `maestro ask` with a `conflict-$KEY-<n>` qid), tests are
green, and the branch is pushed with `set-phase awaiting-ci` appended; or (d) tests did not
converge within this session and you appended `maestro fail` naming why, or `impl-turn` parked the
ticket on the `max_impl_turns` ceiling (it has already called `ops.fail` itself — nothing further
to append). In every case, `maestro release "$KEY"` has run.
