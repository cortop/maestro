---
description: Reconcile an `awaiting-human` maestro ticket — apply the human's answer and route onward. (maestro self-dev)
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` — awaiting-human (self-development)

You are the reconciler for ticket **`$1`** of the maestro project, spawned because it is
currently in the `awaiting-human` phase. Take **exactly ONE** step toward its desired state,
record it only through the `maestro` CLI, then exit. The dispatcher re-spawns you next sweep,
routing to whichever phase file matches the ticket's phase at that time — this file only ever
handles `awaiting-human`.

## Always: load state first
Resolve this ticket's bound repo and the board-wide home as literals — this preamble runs no
`eval`, `python3`, `sed`, or `cat`. REPO/SLUG/BASE/PREFIX/MODE/KIND come from `maestro env --key`,
which can differ per ticket in a multi-repo home (single-repo homes fall back to the legacy
`repo_path`/`branch_prefix` config, so this is unchanged there) — plus MHOME, which is board-wide
and comes from the key-less `maestro env`:
```bash
KEY="$1"
maestro env --key "$KEY"   # -> repo_path/slug/base_branch/branch_prefix/mode/kind/reconcile_command
maestro env                # -> home (board-wide; keyless)
maestro observe-spec "$KEY"
maestro snapshot "$KEY"                     # -> phase, pr, ci, failure_count, open_questions
```
Read the two JSON outputs above and hold their fields as literals for the rest of this file: REPO
(`repo_path`), SLUG (`slug`), BASE (`base_branch`), PREFIX (`branch_prefix`), MODE (`mode`), KIND
(`kind`) from the first call; MHOME (`home`) from the second. Then, with the **Read** tool — never
`cat`/`sed`, this preamble reads no file via the shell — load:
- `<MHOME>/tickets/<KEY>/spec.md` — desired state (you never edit this)
- `<MHOME>/derived/context/<KEY>.md` — folded log: verbatim Q&A, phase reasons, failures, CI
  history, recent impl steps, dependsOn phases — read this before acting, it saves re-deriving
  context from raw events. It may not exist yet for a brand-new ticket; a Read error there just
  means no context has been folded yet, not a failure.

If the snapshot shows pending inbox commands, fold them before deciding:
`maestro fold-inbox "$KEY"`. Finish every exit path with `maestro release "$KEY"` (drop your claim).

## `awaiting-human`: apply the answer, then route onward
You only ran because an answer arrived (already folded above). Read `answered_questions` from
the snapshot you already read in the preamble — it persists across crashes, so it's reliable even
if `observed_seq` has already advanced past the `QuestionAnswered` events. A frontier round
answered only in part wakes you on the first answer — act only on the qids present in
`answered_questions` below; anything still in `open_questions` just stays open for a later wake.
Inspect each qid key in `answered_questions`:
- **any qid starts with `conflict-`** → an escalated merge conflict the implementing reconciler
  could not auto-resolve; the human answered with guidance. Route back to apply it:
  `maestro set-phase "$KEY" implementing --reason "retry conflict resolution: <verbatim>"`

**If `KIND == research`** (research approval question — qid starts with `research-approval-`):
Read the proposal at `$MHOME/tickets/$KEY/proposal.md`. Inspect the answer:
- **"needs more"** → route back to researching:
  `maestro set-phase "$KEY" researching --reason "needs more research per human"`
- **"alternative N"** (e.g. "alternative 2") → extract the Nth alternative's section from proposal.md
- **any other approval** (yes/ok/approve/recommended) → use the `## Recommended` section from proposal.md

Then mint the implementation ticket using the chosen approach as intent, synchronously so its key
comes straight back instead of being derived by scraping internal inbox state:
```bash
maestro create "Implement: <research-title-without-Research-prefix>" --kind implementation \
  --intent "<chosen approach text>" \
  --notes "Seeded from $KEY proposal. See tickets/$KEY/proposal.md for full context." \
  --depends-on "$KEY" --json --no-nudge
```
That prints `{"key": "<impl-key>"}` — the auto-assigned key (T-N). Read it and type it literally
into the breadcrumb below (never captured into a shell variable, same as the PR-number pattern in
`maestro-reconcile-implementing.md`), then finalize:
```bash
maestro append "$KEY" --type Note \
  --payload "{\"text\":\"Created implementation ticket <impl-key> from approved proposal\"}" \
  --step-id "note-impl-created-$KEY"
maestro finalize "$KEY"
```
**Then** `maestro inbox-ack "$KEY"` (last — so a crash before this re-reads the answer).

**If `KIND != research`** (standard implementation ticket):
- **approved** (affirmative answer, qid is not a `conflict-`) → `maestro set-phase "$KEY" ready --reason "approved: <verbatim>"`
- **rejected / `discard`** → `maestro set-phase "$KEY" terminating --reason "rejected: <verbatim>"`
- **modified scope** → note it, then `maestro set-phase "$KEY" ready`
**Then** `maestro inbox-ack "$KEY"` (last — so a crash before this re-reads the answer).

If `answered_questions` AND `open_questions` are **both empty**, you were woken as `stranded`
(a phase set to `awaiting-human` with nothing to wait on — the dispatcher wakes these so they
can't sleep forever). Recover by re-deriving the phase so this step still makes progress: if
`pr_number` is set → `maestro set-phase "$KEY" awaiting-ci --requeue 60`, otherwise
`maestro set-phase "$KEY" triaging --reason "stranded recovery"`.

**Done when:** exactly one `set-phase`/`finalize` path above has appended its event (conflict
retry, research-ticket mint + finalize, standard approve/reject/modify, or stranded recovery),
`maestro inbox-ack "$KEY"` has run last for any answer-consuming path, and
`maestro release "$KEY"` has run.
