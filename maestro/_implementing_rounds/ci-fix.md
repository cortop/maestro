## Round: ci-fix — CI failed

The reason starts `CI failing:` (or `PR #<n>: CI failing:` on a stack), with any failure excerpt
after it. This is a **CI-fix round**: fix what failed. Stay inside `allowed_paths`. If the PR is
`CONFLICTING` or the drift policy requires a sync, run `maestro brief "$KEY" --round sync`
for the sync steps first.

1. Run the tests (core, "Tests are the proof"); re-run `verify-ac` for each AC (core).
2. Snapshot `pr_draft` decides the hand-off. **On a still-draft PR** (`pr_draft` true) record the
   turn first — it bounds the implementing↔qa ping-pong:
   ```bash
   maestro impl-turn "$KEY" --role implementer
   ```
   The verb numbers the turn itself; never hand-roll it with `maestro append`. If its response
   shows the ticket was parked, stop, do not push or hand off, and exit. Skip this on a ready PR.
3. Commit and push:
   ```bash
   git -C <WT> add -A
   git -C <WT> commit -q -m "$KEY: <subject>"
   git -C <WT> push -q -u origin "<PREFIX>$KEY"
   ```
   ```bash
   maestro handoff "$KEY" --summary "<what you did, what you addressed or declined, and why>"
   ```
   `handoff` sends a still-draft PR to `qa` (the fix is a new diff nobody has judged yet) and a
   ready PR to `awaiting-ci` (it is already out for review).
4. Refresh the PR description: `gh pr view <n> --repo "<SLUG>" --json title,body` and compare with
   `git -C <WT> diff <base>...<head>`. Fix only what the new commits made wrong via `gh pr edit
   <n> --repo "<SLUG>" --title ... --body ...`: keep human-written lines verbatim, replace
   AC-to-evidence rows from `ac_verified`. Edit nothing if it still matches. On a stack, refresh
   only entries whose own diff changed, each from its own base...branch diff.
