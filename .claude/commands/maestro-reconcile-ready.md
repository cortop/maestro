---
description: Reconcile a `ready` maestro ticket — honor dependsOn, then start research or set up a worktree. (maestro self-dev)
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` — ready (self-development)

You are the reconciler for ticket **`$1`** of the maestro project, spawned because it is
currently in the `ready` phase. Take **exactly ONE** step toward its desired state, record it
only through the `maestro` CLI, then exit. The dispatcher re-spawns you next sweep, routing to
whichever phase file matches the ticket's phase at that time — this file only ever handles
`ready`.

## Always: load state first
Resolve this ticket's bound repo and the board-wide home as literals — this preamble runs no
`eval`, `python3`, `sed`, or `cat`. REPO/SLUG/BASE/PREFIX/MODE come from `maestro env --key`,
which can differ per ticket in a multi-repo home (single-repo homes fall back to the legacy
`repo_path`/`branch_prefix` config, so this is unchanged there) — plus MHOME, which is board-wide
and comes from the key-less `maestro env`. `MODE` is `git` (default — worktree/branch/PR) or
`local` (AD-6 — a plain directory target, e.g. a notes vault or `~/.claude`, with no branch/PR
path):
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
- `<MHOME>/tickets/<KEY>/spec.md` — desired state (you never edit this)
- `<MHOME>/derived/context/<KEY>.md` — folded log: verbatim Q&A, phase reasons, failures, CI
  history, recent impl steps, dependsOn phases — read this before acting, it saves re-deriving
  context from raw events. It may not exist yet for a brand-new ticket; a Read error there just
  means no context has been folded yet, not a failure.

If the snapshot shows pending inbox commands, fold them before deciding:
`maestro fold-inbox "$KEY"`. Finish every exit path with `maestro release "$KEY"` (drop your claim).

## `ready`: honor dependsOn, then start the next phase
Honor `dependsOn` in the spec: if any listed ticket isn't `done`, sleep
`maestro requeue "$KEY" 300` and exit. Otherwise branch on the ticket kind:

**If `kind == research`** (no worktree needed):
```bash
maestro set-phase "$KEY" researching --reason "research ticket: beginning exploration"
```

**If `kind != research`** (implementation):
- **`MODE == local`** (AD-6 — a plain, non-git target directory): no branch, no worktree, nothing
  to fetch — the reconciler edits `$REPO` (the resolved target dir) directly.
  ```bash
  maestro set-phase "$KEY" implementing --reason "local target ready"
  ```
- **`MODE == git`** (default — create a worktree): `maestro worktree ensure` (GA-20) is a real,
  idempotent Python op, not prose — it creates the worktree off `origin/$BASE` (or adopts the
  branch if one already exists), mirrors GA-7's gitignored guidance (`CLAUDE.local.md`,
  `.claude/settings.local.json`) and `node_modules` from `$REPO` into the worktree as a real,
  write-isolated copy (never a shared symlink — an install run inside the worktree never writes
  through into `$REPO` or a sibling worktree), and runs the resolved repo's config-declared
  `prime` command exactly once (cwd = the new worktree, `$WT`/`$REPO`/`$KEY` in its environment) —
  a fresh worktree otherwise silently lacks any installed dependency tree. A ticket that edits
  `package.json` or a lockfile is priming against a tree that predates its own change; re-run the
  real install inside the worktree first in that case.
  ```bash
  maestro worktree ensure "$KEY"
  maestro set-phase "$KEY" implementing --reason "worktree ready"
  ```
  `ensure` can instead REFUSE (T-81): a witness worktree that already completed creation but
  now fails its health check (non-zero exit, no event) — its error text names a force-remove
  command, but do **not** run it or otherwise remove the worktree yourself; it may hold
  uncommitted work. Escalate instead and exit:
  ```bash
  maestro ask "$KEY" "worktree ensure refused: <detail>" --qid "wt-$KEY"
  ```

**Done when:** either you slept via `maestro requeue "$KEY" 300` (an unmet dependency — nothing
else runs this step), you asked via `maestro ask ... --qid "wt-$KEY"` (`worktree ensure`
refused — nothing else runs this step), or you appended exactly one `set-phase` event
(`researching`, or `implementing` once local mode needs no setup or the git worktree/branch
exists), and `maestro release "$KEY"` has run.
