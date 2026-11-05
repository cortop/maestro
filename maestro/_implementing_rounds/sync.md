## Sync with the base branch (conflicts and base drift)

You are here because `check-conflicts` found the PR `CONFLICTING` (reason says so), or because a
drifted-behind-base worktree was rerouted (reason says `origin/<BASE> advanced (policy=...)`).

**If `STACK_TOOL == gt`:** `gt sync` incorporates the PR branch's own remote state and any moved
trunk in one step — no separate `git fetch`/`--ff-only` merge first:
```bash
gt --cwd <WT> sync
gt --cwd <WT> restack
```
A conflicting restack exits non-zero and leaves conflict markers in the tree — resolve them, then
continue with `gt --cwd <WT> continue` (its own, single invocation; never `gt --cwd <WT> abort`).
Never fall back to `git rebase`/`git merge` on a gt-managed branch (`<PREFIX>$KEY-*`/`<PREFIX>$KEY`)
— gt's parent-tracking would then disagree with the tree.

**If `STACK_TOOL == git` (default):** first fetch and incorporate `origin/<PREFIX>$KEY` — the PR
branch as it stands on GitHub — **before** syncing with base, or a commit pushed by someone else
since your last sync (a CI bot's auto-fix, a human, a co-author) is silently dropped. Force-push is
denied, so a pushed branch can only advance by merge:
```bash
git -C <WT> fetch -q origin "<PREFIX>$KEY"
git -C <WT> merge -q --ff-only "origin/<PREFIX>$KEY"   # no-op if nothing new is there
```
Whether `origin/<PREFIX>$KEY` exists (that fetch succeeds) decides how you sync with base — not
whether `pr_number` is set. If the fetch fails because the branch was never pushed, skip the
`--ff-only` merge too. If that merge fails (local tip and `origin/<PREFIX>$KEY` diverged),
reconcile instead of discarding either side: `git -C <WT> merge -q "origin/<PREFIX>$KEY"` and
resolve any conflict as below.

Then sync with the latest base and resolve any conflicts — always resolve, never
`git merge --abort` and never `git rebase --abort`. Fetch the latest base first:
```bash
git -C <REPO> fetch -q origin "<BASE>"
```

**Pushed branch (`origin/<PREFIX>$KEY` exists)** — merge base in; never rebase, since the rebased
branch could not be published:
```bash
git -C <WT> merge -q "origin/<BASE>"
```
A conflicting merge exits non-zero and leaves conflict markers in the tree — resolve them, then
finish with `git -C <WT> commit --no-edit` (its own, single invocation; never `git merge --abort`).

**Never-pushed branch** — rebase onto base:
```bash
git -C <WT> rebase "origin/<BASE>"
```
A conflicting rebase exits non-zero and leaves conflict markers in the tree — resolve them, then
continue with `git -C <WT> rebase --continue` (its own, single invocation; never
`git rebase --abort`).

**Conflicted generated file:** take either side of it, resolve every other conflicted file first,
then run the repo's generator (the repo's CLAUDE.md/AGENTS.md names it and the generated files)
and `git add` its output — never hand-merge generated text. Do not run the generator through
`make`: `make` is not granted to reconcilers, and a denied call leaves the worktree mid-merge,
which trips the dispatcher's repo_preflight merge-in-progress check.

Before resolving a conflicting hunk, recover the intent on **both** sides — read the commit(s)
(and, if the subject references one, the PR/ticket) that introduced the conflicting lines on
`origin/<BASE>` (`git log -1 --format='%H %s' <sha>`, `gh pr view <n>` if it names a PR number),
alongside this ticket's own spec Intent — so the merge reconciles what both sides were trying to
do, not just a textual splice.
