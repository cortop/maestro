## Round: qa-fix — `qa` sent the ticket back

The most recent transition is `qa -> implementing`, citing a failing AC and evidence
(`maestro snapshot "$KEY"` -> `qa_verdicts` has the raw record for the same ac_hash). This is a
**fix round**, not a fresh implementation: fix the code per that evidence. Do not re-derive the
diff or re-judge the AC yourself — that is `qa`'s independent job; yours is to fix and hand off.
Stay inside `allowed_paths`.

1. Run the tests (core, "Tests are the proof"); re-run `verify-ac` for each AC (core).
2. Record the turn — this bounds the implementing↔qa ping-pong:
   ```bash
   maestro impl-turn "$KEY" --role implementer
   ```
   The verb numbers the turn and mints its own step-id — never hand-roll it with `maestro
   append`. If its response shows the ticket was parked (past `max_impl_turns`), stop here, do
   not push or hand off, and exit.
3. Commit and push (the PR already has its `PrOpened` event — do not append another), then hand
   off:
   ```bash
   git -C <WT> add -A
   git -C <WT> commit -q -m "$KEY: <subject>"
   git -C <WT> push -q -u origin "<PREFIX>$KEY"
   maestro set-phase "$KEY" qa
   ```
4. Refresh the PR description: `gh pr view <n> --repo "<SLUG>" --json title,body` and compare with
   `git -C <WT> diff <base>...<head>`. Fix only what the new commits made wrong via `gh pr edit
   <n> --repo "<SLUG>" --title ... --body ...`: keep human-written lines verbatim, replace
   AC-to-evidence rows from `ac_verified`. Edit nothing if it still matches. On a stack, refresh
   only entries whose own diff changed, each from its own base...branch diff.
