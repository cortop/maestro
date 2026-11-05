## Round: review-feedback — a PR reviewer sent the ticket back

The reason is `changes requested: <body>`, `review comment: <body>` or `approved with comments:
<body / path:line: inline comments>`; the verbatim text is right there. Evaluate each comment on
its merits — address it with a real code change where warranted, or say why not. Stay inside
`allowed_paths`.

A review reason alone is not a reason to sync with base: every merge plus push restarts CI and
invites another review round. Sync only when the PR is `CONFLICTING` or the drift policy requires
it (`maestro brief "$KEY" --round sync` prints the steps).

**Non-blocking AI-reviewer suggestions are optional.** When the comment comes from an automated AI
reviewer and is a non-blocking suggestion (a nit, a style preference, a lower-priority idea), you
may decline it: record the reason, push nothing, and go straight to `maestro handoff "$KEY"`.
Never decline this way a `changes requested:` review or any comment written by a human.

**`approved with comments:`** means the reviewer already APPROVED: the approval is NOT blocking,
so never treat it as a reason to stop or escalate. Evaluate EACH comment (the approval body and
every `path:line:` inline comment, separated by ` | `), address what is warranted, and reply only
to the inline comments you addressed or declined with a reason.

Reply only to an **inline** comment (`comment_id` of the form `inline-<id>` on the round's
`ReviewFeedbackReceived` event; `maestro events "$KEY"` lists them) that asked for something, and
only once you addressed it or declined it with a reason — in its own thread, never a new
top-level `gh` comment:
`maestro reply-review "$KEY" --comment-id inline-<id> --body "<what changed or why not, in 1-3
plain sentences, plus the commit sha>"`. The verb refuses a non-inline id: address a review body
or plain PR comment with a code change, or record a Note if you decline it. Never reply to a plain
approval, an "LGTM", or an acknowledgement. It also refuses a body over ~600 chars or one carrying
internal jargon (a phase name, an AC-hash, a step id), and is idempotent per (comment, tree state).

1. If you made a code change, run the tests (core, "Tests are the proof").
2. Commit and push (skip the push entirely if this round only recorded a Note), then hand back to
   CI and that review (`handoff` picks `awaiting-ci`) — a human is already reviewing the PR on GitHub, so not `qa`:
   ```bash
   git -C <WT> add -A
   git -C <WT> commit -q -m "$KEY: <subject>"
   git -C <WT> push -q -u origin "<PREFIX>$KEY"
   maestro handoff "$KEY"
   ```
3. After a code push, refresh the PR description: `gh pr view <n> --repo "<SLUG>" --json
   title,body` and compare with `git -C <WT> diff <base>...<head>`. Fix only what the new commits
   made wrong via `gh pr edit <n> --repo "<SLUG>" --title ... --body ...`: keep human-written
   lines verbatim, replace AC-to-evidence rows from `ac_verified`. Edit nothing if it still
   matches; skip it on a Note-only round. On a stack, refresh only entries whose own diff changed.
