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

The `implementing` reconciler (`skills/maestro-reconcile-implementing.md`) hands off here itself
(RF-7) once it has tests green, every AC self-attested, and a PR open — via `set-phase qa`, never
via an in-session `Agent`-tool sub-agent. Treat every spawn here as a live QA step:

## Always: load state first
Resolve this ticket's bound repo and the board-wide home as literals — this preamble runs no
`eval`, `python3`, `sed`, or `cat`. REPO/SLUG/BASE/PREFIX/MODE come from `maestro env --key`,
which can differ per ticket in a multi-repo home (single-repo homes fall back to the legacy
`repo_path`/`branch_prefix` config, so this is unchanged there) — plus MHOME and
QA_STANDARDS_AXIS, which are board-wide and come from the key-less `maestro env`:
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

   **Standards axis (T-23, config-gated by `qa_standards_axis`, default off; RF-7 moved this here
   from `implementing`).** If `QA_STANDARDS_AXIS == true`, spawn a second **Standards QA**
   sub-agent (`Agent` tool) — the one legal fan-out this phase allows, priced into
   `dispatcher.spawn_weight(cfg, "qa")`. Brief it with CLAUDE.md's conventions (one-line module
   docstrings, stdlib-only core, never hand-edit `derived/`, QA proves the feature with the real
   app and mocks only the external boundary, mount the real app for TUI changes) plus a
   Fowler-smell baseline (long methods, duplicated code, large classes, feature envy, shotgun
   surgery), and the same qa-brief diff — not the AC list, and not your own spec-axis findings.
   Its job, per spec AC touched by the diff: judge PASS or FAIL against those standards,
   independent of your own spec-axis verdict. Record with `maestro qa-verdict "$KEY" --ac <n>
   --verdict pass|fail --axis standards --evidence "<smell or convention violated, or why it's
   clean>"`. Standards findings are recorded but **never reranked against, or merged with, the
   spec-axis findings** — they land in a separate snapshot bucket (`qa_verdicts_standards`) and
   are advisory only: unlike a spec-axis fail, a Standards-axis fail does not block `set-phase
   awaiting-ci` (explicit, tested choice — see `ops._refuse_if_qa_failing`,
   `tests/test_standards_qa_axis.py`). Do not fold Standards findings into the routing decision in
   step 3 below; if they're worth a human's attention, leave a `maestro append --type Note`
   breadcrumb and proceed.
3. Route on the outcome:
   - **every AC PASS** → `maestro set-phase "$KEY" awaiting-ci --reason "qa: all ACs pass"`
     (only valid once a PR is actually open — if this ticket reached `qa` before a PR existed,
     route to `implementing` instead so it can open one).
   - **any AC FAIL** → `maestro set-phase "$KEY" implementing --reason "qa: <ac> failed:
     <evidence>"` so the implementer can fix it and re-request review.

**Done when:** every current-hash AC in the qa-brief packet has a `qa-verdict`, and `set-phase`
has routed the ticket onward (`awaiting-ci` on all-pass, `implementing` on any fail) with a
reason citing the verdicts. Then `maestro release "$KEY"` has run.
