## Sync with the base branch (conflicts and base drift)

You are here because `check-conflicts` found the PR `CONFLICTING` (reason says so), or because a
drifted-behind-base worktree was rerouted (reason says `origin/<BASE> advanced (policy=...)`).

```bash
maestro sync-base "$KEY"
```
The verb owns the mechanics: it incorporates `origin/<PREFIX>$KEY` first, then merges base into a
pushed branch or rebases a never-pushed one (on `STACK_TOOL == gt` it runs `gt sync` and `gt
restack` instead, never plain git). It prints `status` `clean` (continue below) or `conflicted`
with `conflicted_files` and a `continue_command`. Resolve the files, `git add` them, run the
`continue_command` as its own single invocation, then re-run `maestro sync-base "$KEY"` until it
reports `clean`. Always resolve — never `git merge --abort`, `git rebase --abort` or `gt abort`.

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
