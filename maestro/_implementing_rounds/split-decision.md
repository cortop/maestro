## Round: split-decision — the PR-split proposal was answered

The reason starts `pr split decision:` and carries the verbatim answer. The code was already
implemented and the tests were green before you asked, so there is nothing new to commit and no
PR exists yet. Act on the answer: an approving answer means open the approved stack (below);
anything else means push and open the single PR (`maestro brief "$KEY" --round fresh` prints the
`exceeds: false` steps; `pr-size` is not re-run).

Each stack entry's PR is its own review unit: its title subject and body describe only that
entry's diff (its base...its branch), never the ticket-wide subject reused under a new `(n/N)`.
Its AC-to-evidence table lists only the ACs the approved proposal gave that entry; one clause
pointing at the neighbouring PRs is enough.

**Approved split, if `STACK_TOOL == gt`:** build each entry as its own gt-tracked branch, oldest
first, each created while checked out on the previous entry's branch so gt records the parent link:
```bash
git -C <WT> log --oneline "origin/<BASE>..HEAD"   # find each entry's last commit
git -C <WT> checkout -q <sha of entry 1's last commit>
gt --cwd <WT> create "<PREFIX>$KEY-1" -m "$KEY: <entry 1 subject> (1/<N>)"
git -C <WT> checkout -q <sha of entry 2's last commit>
gt --cwd <WT> create "<PREFIX>$KEY-2" -m "$KEY: <entry 2 subject> (2/<N>)"
```
Repeat for entries `2..N` (the last entry may just be `<PREFIX>$KEY` at `HEAD`), then submit and
open every PR in one call — the ONLY path allowed to push a gt-managed branch with history
rewritten:
```bash
gt --cwd <WT> submit --stack --no-interactive --publish
```
Record the entries exactly as the `git` path does below — `gt submit` already pushed and opened
the PRs, so never run `git push`/`gh pr create` yourself. `gt submit` names PRs from their commits,
so before appending, `gh pr edit` each PR to its own `<DISPLAY_KEY>: <entry n subject> (<n>/<N>)`
title and per-entry body.

**Approved split, if `STACK_TOOL == git` (default):** label each entry on the branch's linear
history (no rewrite) and open its PR oldest-first, each based on the previous entry's branch,
never force-pushed:
```bash
git -C <WT> log --oneline "origin/<BASE>..HEAD"   # find each entry's last commit
git -C <WT> branch "<PREFIX>$KEY-1" <sha of entry 1's last commit>
git -C <WT> push -q -u origin "<PREFIX>$KEY-1"
gh pr create --repo "<SLUG>" --base "<BASE>" --head "<PREFIX>$KEY-1" --draft --title "<DISPLAY_KEY>: <entry 1 subject> (1/<N>)" --body "<entry 1 body>"
```
Repeat for entries `2..N`, each based on `<PREFIX>$KEY-<n-1>` instead of `<BASE>` (title
`<DISPLAY_KEY>: <entry n subject> (<n>/<N>)`, never `$KEY`); the last entry may just be
`<PREFIX>$KEY` at `HEAD`. Record entry 0 — the only PR QA/CI/review poll directly — with a
`stack` sub-payload, and every later entry with the same event type (`ops.check_merged` advances
`pr_number`/`pr_url` down the stack as each entry merges):
```bash
maestro append "$KEY" --type PrOpened --payload "{\"number\":<pr1-number>,\"url\":\"<pr1-url>\",\"draft\":true,\"stack\":{\"index\":0,\"total\":<N>,\"branch\":\"<PREFIX>$KEY-1\",\"base\":\"<BASE>\"}}" --step-id "pr-$KEY"
maestro append "$KEY" --type PrOpened --payload "{\"number\":<pr2-number>,\"url\":\"<pr2-url>\",\"draft\":true,\"stack\":{\"index\":1,\"total\":<N>,\"branch\":\"<PREFIX>$KEY-2\",\"base\":\"<PREFIX>$KEY-1\"}}" --step-id "pr-stack-$KEY-1"
maestro set-phase "$KEY" qa
```
One `PrOpened` append per entry, `index` 0-based, `step-id` `"pr-$KEY"` for index 0 and
`"pr-stack-$KEY-<index>"` for every later one, so a crash-and-respawn never double-opens a PR.
**Open and append strictly root-first**: `maestro append` refuses a `PrOpened` at `stack.index =
n > 0` unless entry `n-1` is already recorded.

Push normally — never force-push. Then exit; the next sweep spawns the independent `qa` reconciler.
