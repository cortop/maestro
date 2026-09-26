---
description: Reconcile an `implementing` maestro ticket — code the ACs, self-review, open a PR, hand off to independent QA. (maestro self-dev)
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` — implementing (self-development)

You are the reconciler for ticket **`$1`** of the maestro project, spawned because it is
currently in the `implementing` phase. Take **exactly ONE** step toward its desired state,
record it only through the `maestro` CLI, then exit. The dispatcher re-spawns you next sweep,
routing to whichever phase file matches the ticket's phase at that time — this file only ever
handles `implementing`. QA is a separate, independent phase (`qa`) the dispatcher spawns on its
own next sweep once you hand off below — never something this session spawns itself.

## Always: load state first
Resolve this ticket's bound repo and the board-wide home as literals — this preamble runs no
`eval`, `python3`, `sed`, or `cat`. REPO/SLUG/BASE/PREFIX/MODE/DISPLAY_KEY come from `maestro env
--key`, which can differ per ticket in a multi-repo home (single-repo homes fall back to the
legacy `repo_path`/`branch_prefix` config, so this is unchanged there) — plus MHOME and
QA_STANDARDS_AXIS, which are board-wide and come from the key-less `maestro env`. `MODE` is `git`
(default — worktree/branch/PR, the rest of this doc unless said otherwise) or `local` (AD-6 — a
plain directory, e.g. a notes vault or `~/.claude` for self-editing skills, with no branch/PR
path; called out explicitly below wherever it changes what you do). `DISPLAY_KEY` (T-134) is the
tracker's own identifier (e.g. `BDA-123`) for a tracker-imported ticket, or `KEY` unchanged
otherwise — use it for every PR title below; `KEY` itself, branch names, step-ids and event
payloads always stay the maestro key, never `DISPLAY_KEY`:
```bash
KEY="$1"
maestro env --key "$KEY"   # -> repo_path/slug/base_branch/branch_prefix/mode/display_key/reconcile_command
maestro env                # -> home/qa_standards_axis (board-wide; keyless)
maestro observe-spec "$KEY"
maestro snapshot "$KEY"                     # -> phase, pr, ci, failure_count, open_questions
```
Read the two JSON outputs above and hold their fields as literals for the rest of this file: REPO
(`repo_path`), SLUG (`slug`), BASE (`base_branch`), PREFIX (`branch_prefix`), MODE (`mode`),
DISPLAY_KEY (`display_key`) from the first call; MHOME (`home`) and QA_STANDARDS_AXIS
(`qa_standards_axis`) from the second. Then,
with the **Read** tool — never `cat`/`sed`, this preamble reads no file via the shell — load:
- `<MHOME>/tickets/<KEY>/spec.md` — desired state (you never edit this)
- `<MHOME>/derived/context/<KEY>.md` — folded log: verbatim Q&A, phase reasons, failures, CI
  history, recent impl steps, dependsOn phases — read this before acting, it saves re-deriving
  context from raw events. It may not exist yet for a brand-new ticket; a Read error there just
  means no context has been folded yet, not a failure. **This is also how you tell a fix round
  from a fresh implementation** — if the most recent phase-history line reads `qa -> implementing`
  with a reason citing a failing AC, `qa` sent you back; see step 1 below. If instead it reads
  `in-review -> implementing` or `awaiting-ci -> implementing` with a reason starting `changes
  requested:`, `review comment:` or `approved with comments:`, a PR reviewer's feedback sent you back — see the review-
  feedback case in step 1 below, a different fix round from the `qa` one. A third shape,
  `awaiting-human -> implementing` with a reason starting `pr split decision:`, means a PR-split
  proposal (T-126) was just answered — see step 1's fourth case.

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
step 5 (push the rebased branch + `set-phase awaiting-ci`); the `pr-size` check does not apply
here (T-129) — it only ever runs before the first PR exists, never on a conflict-only pass against
one already open. Keep the prior attestations and QA
verdicts as-is (the spec, and so their content hashes, didn't change) — do not re-implement the
feature, re-run `verify-ac`, or route the ticket through `qa` for this pass. `set-phase
awaiting-ci` enforces the same write-path gates here as everywhere else (T-85: every current-hash
AC needs a passing spec-axis `qa-verdict`, recorded from the `qa` phase) — since the spec didn't
change, the ticket's existing verdicts and attestations already satisfy them, so this call
succeeds with no extra step; if it unexpectedly refuses (a prior pass never actually reached a
clean `qa` verdict), fix that per its error rather than forcing past it — `--force` does not
override the QA gates. If you truly cannot reconcile the two intents yourself, escalate:
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
   5).
   **If a PR reviewer sent you back instead** (the most recent phase-history transition is
   `in-review -> implementing` or `awaiting-ci -> implementing`, reason `changes requested: <body>`
   `review comment: <body>` or `approved with comments: <body / path:line: inline comments>` — the
   verbatim comment text is right there in the reason): this is
   a **review-feedback round**, a third case alongside the `qa` fix round and a fresh
   implementation. Evaluate the comment on its merits — address it with a real code change where
   it's warranted, or, if no change is warranted, say why. Either way, reply to it **in its own
   thread** through `maestro reply-review` (T-128) — never a local Note alone, and never an
   improvised new top-level `gh` comment:
   `maestro reply-review "$KEY" --comment-id <id> --body "<what changed or why not, in 1-3 plain
   sentences, plus the commit sha>"`. `<id>` is that comment's own `comment_id`, off the
   `ReviewFeedbackReceived` event this round recorded for it (`maestro events "$KEY"` lists them) —
   an `inline-<id>` threads the reply under the original inline comment; any other id (a review
   body or a plain PR comment) gets one quoting PR comment instead, both decided by the verb
   itself, never by you. The verb refuses a body over ~600 chars or one carrying internal jargon
   (a phase name, an AC-hash, a step id) — write it for the reviewer, not for another reconciler.
   It is also idempotent per (comment, tree state): calling it again for a comment you already
   answered at this same commit is a no-op, so replying before you know whether this pass
   converges is safe. Either way continue at step 2, then at step 5 push the fix (or, if you made
   no code change, just the reply — nothing to push) and `set-phase awaiting-ci` instead of
   `set-phase qa`: a human is already reviewing this PR directly on GitHub, so this pass hands back
   to CI/that review rather than routing through `qa` again. **`approved with comments:`** means
   the reviewer already APPROVED the PR but left feedback with it: the approval is NOT blocking,
   so never treat it as a reason to stop or escalate. Evaluate EACH comment (the approval body and
   every `path:line:` inline comment, separated by ` | `) — address what is warranted with a real
   change, and reply to EVERY one (addressed or declined) with `maestro reply-review`, same as
   above, so the human sees the evaluation in the thread itself rather than a Note only they'd have
   to go dig for.
   **If a PR-split proposal was answered instead** (the most recent phase-history transition is
   `awaiting-human -> implementing` with a reason starting `pr split decision:` — the verbatim
   answer is right there in the reason): this is a **split-decision round** (T-126), not a fresh
   implementation or a fix — the code was already fully implemented and tests were already green
   before you asked. Skip straight to step 5's size-gated PR step below, which reads this same
   answer to decide between the approved stack and the single PR you deferred.
   Otherwise, make the change fresh.
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
   created, so this step only runs the tests. Run the suite as a single **foreground** Bash call
   with an explicit timeout of 1800000ms (30 min — the dispatcher exports `BASH_MAX_TIMEOUT_MS`
   from `[maestro] bash_max_timeout` so the Bash tool accepts it; the stock 600000ms ceiling
   is too short for this suite) — never `run_in_background`, never
   `ScheduleWakeup`/`Monitor`, and never a sleep/tail poll loop on a test log:
   ```bash
   .venv/bin/python -m pytest -q
   ```
   **Pytest's permission story, decided:** `.venv/bin/python` stays cwd-anchored above, never
   absolutized to an absolute path rooted at `<WT>` — the reconciler's own Bash permission grant
   matches only the *relative* prefix `.venv/bin/`, which matches the command string solely while the
   shell's cwd is the worktree, and `dispatcher._worker_cwd`
   (`maestro/dispatcher.py:1369`) already runs this session with `<WT>` as cwd — so the relative
   form is both correct and the only one that avoids a permission prompt. Do not "fix" this back
   to an absolute path.
   If red, fix and re-run — stay on this step until green. If the suite cannot complete inside
   that foreground timeout budget, do not exit to wait for it — run
   `maestro fail "$KEY" "suite exceeds tool timeout: <why>"` and exit; the dispatcher-owned
   `verifying` stage (once `test_command` is armed) is the place for long runs, not this
   session. If you exceed ~`max_impl_turns` edit/test cycles without converging: `maestro fail
   "$KEY" "non-converging: <why>"` and exit.
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
5. Commit your work:
   ```bash
   git -C <WT> add -A
   git -C <WT> commit -q -m "$KEY: <subject>"
   ```
   **If a PR is already open** (a fix round, a review-feedback round, or a conflict-only pass —
   the snapshot you read at the top already has `pr_number`/`pr_stack` set): the `pr-size` gate
   (T-126) does not apply — it only ever runs before the *first* PR exists (T-129), since pushing
   a fix past the split threshold must never trigger a split proposal against a PR reviewers are
   already working. Skip straight to pushing and handing off, with no `maestro pr-size` call at
   all (calling it anyway would be a harmless no-op — `ops.pr_size` itself now refuses to report
   `exceeds: true` once a PR is open — but there is no reason to call it here):
   ```bash
   git -C <WT> push -q -u origin "<PREFIX>$KEY"
   ```
   - **Fix round**: `maestro set-phase "$KEY" qa --requeue 300` (a PR already open already has its
     `PrOpened` event — do not append another).
   - **Review-feedback round**: `maestro set-phase "$KEY" awaiting-ci --requeue 300` instead of
     `qa` — a human is already reviewing this PR directly on GitHub, so this hands back to
     CI/that review rather than an internal re-review; push the commit first if you made a code
     change, or skip the push entirely if this round only recorded a Note.
   - **Conflict-only pass**: `maestro set-phase "$KEY" awaiting-ci --requeue 300` (see step 0).

   **If this is a split-decision round** (step 1's fourth case): there is nothing new to commit —
   the commit above is a no-op — no PR exists yet at this point either, so act on the verbatim
   answer from the phase-history reason instead: an approving answer means open the approved stack
   (see "Approved split" below); anything else means push and open the single PR exactly as the
   `exceeds: false` case below (the size decision was already made when the split proposal was
   raised, so `pr-size` is not re-run here).

   **Otherwise (a fresh implementation, with no PR yet)** — this is the only case where the gate
   applies: measure the diff and act on `exceeds` before opening the first PR:
   ```bash
   maestro pr-size "$KEY"   # -> {lines_changed, threshold, exceeds, tree}
   ```
   **`exceeds: false`** (including threshold `0`, which disables the check entirely — byte-identical
   to before this ticket) — push and open (or update) the PR exactly as today, with an
   AC-to-evidence table sourced from the `verify-ac` calls above (or `maestro snapshot "$KEY"` ->
   `ac_verified`):
   ```bash
   git -C <WT> push -q -u origin "<PREFIX>$KEY"
   gh pr create --repo "<SLUG>" --base "<BASE>" --head "<PREFIX>$KEY" --draft --title "<DISPLAY_KEY>: <subject>" --body "<motivation/changes> ## AC-to-evidence

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
   resolved from `maestro env --key`):
   ```bash
   maestro append "$KEY" --type PrOpened --payload "{\"number\":<pr-number>,\"url\":\"<pr-url>\",\"draft\":true}" --step-id "pr-$KEY"
   maestro set-phase "$KEY" qa --requeue 300
   ```

   **`exceeds: true`** — do not push or open/grow the PR yet. Split the diff into an ordered stack
   of smaller PRs instead, by file: each entry names which files it touches, which spec ACs it
   covers, and which entry it's based on (entry 1 depends on nothing beyond `<BASE>`; entry *n*
   depends on entry *n-1*). Propose it — `--qid` binds the proposal to this exact tree state, so an
   already-answered proposal for it passes straight through on a later pass and any new tree (a
   further commit) re-evaluates from scratch, same idiom as the H4 test-deletion gate:
   ```bash
   maestro ask "$KEY" "$KEY: this diff is <lines_changed> lines changed (threshold <threshold>) -- proposing a stack instead of one PR -- 1) <files> (AC <n>) based on <BASE> | 2) <files> (AC <n>) based on <PREFIX>$KEY-1 | more entries as needed -- approve to open the stack, or say what to keep as one PR instead." --qid "split-$KEY-<tree>"
   ```
   Then exit (`maestro release "$KEY"`) without pushing — the dispatcher wakes `awaiting-human` on
   the answer, which routes back here (see step 1's fourth case) to act on it.

   **Approved split** — label each stack entry on the branch's already-linear commit history (no
   rewrite) and open its PR oldest-first, each based on the previous entry's branch, never
   force-pushed:
   ```bash
   git -C <WT> log --oneline "origin/<BASE>..HEAD"   # find each entry's last commit
   git -C <WT> branch "<PREFIX>$KEY-1" <sha of entry 1's last commit>
   git -C <WT> push -q -u origin "<PREFIX>$KEY-1"
   gh pr create --repo "<SLUG>" --base "<BASE>" --head "<PREFIX>$KEY-1" --draft --title "<DISPLAY_KEY>: <subject> (1/<N>)" --body "..."
   ```
   repeat for entries `2..N`, each based on `<PREFIX>$KEY-<n-1>` instead of `<BASE>` (the title's
   `<DISPLAY_KEY>: <subject> (<n>/<N>)` for every entry — never `$KEY`), and the last
   entry may just be `<PREFIX>$KEY` at `HEAD` itself — no extra branch needed). Record entry 0 —
   the first PR, the only one QA/CI/review ever poll directly — with the normal `PrOpened` call
   plus a `stack` sub-payload, and every later entry with the same event type (T-126 — see
   `ops.check_merged`, which advances `pr_number`/`pr_url` down the stack as each entry merges,
   finalizing the ticket only once the last one does):
   ```bash
   maestro append "$KEY" --type PrOpened --payload "{\"number\":<pr1-number>,\"url\":\"<pr1-url>\",\"draft\":true,\"stack\":{\"index\":0,\"total\":<N>,\"branch\":\"<PREFIX>$KEY-1\",\"base\":\"<BASE>\"}}" --step-id "pr-$KEY"
   maestro append "$KEY" --type PrOpened --payload "{\"number\":<pr2-number>,\"url\":\"<pr2-url>\",\"draft\":true,\"stack\":{\"index\":1,\"total\":<N>,\"branch\":\"<PREFIX>$KEY-2\",\"base\":\"<PREFIX>$KEY-1\"}}" --step-id "pr-stack-$KEY-1"
   maestro set-phase "$KEY" qa --requeue 300
   ```
   (one `PrOpened` append per entry, `index` 0-based in stack order, `step-id` `"pr-$KEY"` for
   index 0 and `"pr-stack-$KEY-<index>"` for every later one, so a crash-and-respawn mid-step never
   double-opens a PR).
   Push normally — never force-push. Let hooks run — never skip them. Test the real behavior,
   never a mock. Then exit; the dispatcher's next sweep spawns the independent `qa` reconciler
   (`skills/maestro-reconcile-qa.md`) — this session never judges its own diff, and does not poll
   CI itself.

**Done when** one of the following holds: (a) a fresh implementation — tests are green, every spec
AC has a `verify-ac` attestation, a PR is open with `PrOpened` recorded, and `set-phase qa` has
appended, handing review off to the independent `qa` phase; (b) a fix round — the fix is made per
`qa`'s evidence, tests are green, `impl-turn` recorded the round (and did not park the ticket), the
fix is pushed, and `set-phase qa` has appended again to re-request review; (c) a conflict-only
pass — the rebase is clean (or escalated via `maestro ask` with a `conflict-$KEY-<n>` qid), tests
are green, and the branch is pushed with `set-phase awaiting-ci` appended; (d) a review-feedback
round — every addressed or declined comment got a `maestro reply-review` reply in its own thread,
each one either addressed with a pushed code change or explaining why no change is needed, tests
are green if code changed, and `set-phase awaiting-ci` has appended; (e) tests did not converge
within this session
and you appended `maestro fail` naming why, or `impl-turn` parked the ticket on the
`max_impl_turns` ceiling (it has already called `ops.fail` itself — nothing further to append);
(f) an oversized diff (T-126) — `pr-size` reported `exceeds: true` and `maestro ask` recorded a
`split-$KEY-<tree>` proposal, with nothing pushed this pass; or (g) a split-decision round — the
human's answer was read and either the approved stack was opened (one `PrOpened` per entry,
`set-phase qa` appended) or the single PR was opened as in (a) after a decline.
In every case, `maestro release "$KEY"` has run.
