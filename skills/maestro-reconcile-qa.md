---
description: Reconcile a `qa` maestro ticket — adversarial review of a diff, no Edit/Write. (maestro self-dev)
allowed-tools: Bash, Read, Glob, Grep, Agent
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` — qa (self-development)

You are the reconciler for ticket **`$1`** of the maestro project, spawned because it is
currently in the `qa` phase. Take **exactly ONE** step toward its desired state, record it only
through the `maestro` CLI, then exit. The dispatcher re-spawns you next sweep, routing to
whichever phase file matches the ticket's phase at that time — this file only ever handles `qa`.

This phase exists to keep a QA judgment independent of the agent that wrote the code it is
judging: `dispatcher.phase_denylist` denies this session `Edit` and `Write` at spawn time (RF-6),
mechanically — not by convention — so nothing you do here can touch the diff under review. If
your work here concludes the diff needs a change, say so in a verdict or a `maestro ask`; do not
attempt to route around the denylist.

**RF-6 is additive only: nothing routes a ticket into `qa` yet** — the `implementing` reconciler
still runs its adversarial QA loop entirely in-session (`maestro qa-brief` /
`maestro qa-verdict`, see `.claude/commands/maestro-reconcile-implementing.md`) and never sets
`set-phase … qa`. If you were spawned here, either a human moved a ticket into `qa` by hand or a
later ticket has wired real routing into this phase — in both cases, treat it as a live QA step:

## Always: load state first
Resolve this ticket's bound repo and the board-wide home as literals — this preamble runs no
`eval`, `python3`, `sed`, or `cat`. REPO/SLUG/BASE/PREFIX/MODE come from `maestro env --key`,
which can differ per ticket in a multi-repo home (single-repo homes fall back to the legacy
`repo_path`/`branch_prefix` config, so this is unchanged there) — plus MHOME, which is board-wide
and comes from the key-less `maestro env`:
```bash
KEY="$1"
maestro env --key "$KEY"   # -> repo_path/slug/base_branch/branch_prefix/mode/reconcile_command
maestro env                # -> home (board-wide; keyless)
maestro observe-spec "$KEY"
maestro snapshot "$KEY"                     # -> phase, pr, ci, failure_count, open_questions
```
Read the two JSON outputs above and hold their fields as literals for the rest of this file: REPO
(`repo_path`), SLUG (`slug`), BASE (`base_branch`), PREFIX (`branch_prefix`), MODE (`mode`) from
the first call; MHOME (`home`) from the second. Then, with the **Read** tool — never `cat`/`sed`,
this preamble reads no file via the shell — load:
- `<MHOME>/tickets/<KEY>/spec.md` — desired state, and the ACs you are judging against
- `<MHOME>/derived/context/<KEY>.md` — folded log: verbatim Q&A, phase reasons, failures, CI
  history, recent impl steps, dependsOn phases — read this before acting, it saves re-deriving
  context from raw events. It may not exist yet for a brand-new ticket; a Read error there just
  means no context has been folded yet, not a failure.

If the snapshot shows pending inbox commands, fold them before deciding:
`maestro fold-inbox "$KEY"`. Finish every exit path with `maestro release "$KEY"` (drop your claim).

## `qa`: judge the diff, never edit it
1. `maestro qa-brief "$KEY"` — the deterministic hand-off packet: every spec AC (content-hash
   keyed, the same key `verify-ac`/`qa-verdict` use) plus a diff anchored on the merge-base with
   `<BASE>`, untracked files included via `git diff --no-index`. Read-only; safe to call again.
2. For each AC in the packet, judge PASS or FAIL strictly against what the diff actually does —
   not against your own idea of the right implementation, and without editing anything to check:
   `maestro qa-verdict "$KEY" --ac <n> --verdict pass|fail --evidence "<what you checked, what
   you saw in the diff>"` (1-based, in spec order; defaults to `--axis spec`).
3. Route on the outcome:
   - **every AC PASS** → `maestro set-phase "$KEY" awaiting-ci --reason "qa: all ACs pass"`
     (only valid once a PR is actually open — if this ticket reached `qa` before a PR existed,
     route to `implementing` instead so it can open one).
   - **any AC FAIL** → `maestro set-phase "$KEY" implementing --reason "qa: <ac> failed:
     <evidence>"` so the implementer can fix it and re-request review.

**Done when:** every current-hash AC in the qa-brief packet has a `qa-verdict`, and `set-phase`
has routed the ticket onward (`awaiting-ci` on all-pass, `implementing` on any fail) with a
reason citing the verdicts. Then `maestro release "$KEY"` has run.
