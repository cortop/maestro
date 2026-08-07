---
description: Reconcile a `ready` maestro ticket — honor dependsOn, then start research or set up a worktree. (maestro self-dev)
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` — ready (self-development)

You are the reconciler for ticket **`$1`** of the maestro project, spawned because it is
currently in the `ready` phase. Take **exactly ONE** step toward its desired state, record it
only through the `maestro` CLI, then exit. The dispatcher re-spawns you next sweep, routing to
whichever phase file matches the ticket's phase at that time — this file only ever handles
`ready`.

## Always: load state first
Resolve this ticket's bound repo — REPO/SLUG/BASE/PREFIX/MODE come from `maestro env --key`, which
can differ per ticket in a multi-repo home (single-repo homes fall back to the legacy
`repo_path`/`branch_prefix` config, so this is unchanged there) — plus MHOME, which is board-wide
and comes from the key-less `maestro env`. `MODE` is `git` (default — worktree/branch/PR) or
`local` (AD-6 — a plain directory target, e.g. a notes vault or `~/.claude`, with no branch/PR
path):
```bash
KEY="$1"
eval "$(maestro env --key "$KEY" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("REPO="+(d["repo_path"] or "")+"\nSLUG="+(d["slug"] or "")+"\nBASE="+d["base_branch"]+"\nPREFIX="+d["branch_prefix"]+"\nMODE="+d["mode"])')"
eval "$(maestro env | python3 -c 'import sys,json;print("MHOME="+json.load(sys.stdin)["home"])')"
maestro observe-spec "$KEY"
maestro snapshot "$KEY"                     # -> phase, pr, ci, failure_count, open_questions
sed -n '1,200p' "$MHOME/tickets/$KEY/spec.md"   # desired state (you never edit this)
cat "$MHOME/derived/context/$KEY.md" 2>/dev/null   # folded log: verbatim Q&A, phase reasons,
                                                    # failures, CI history, recent impl steps,
                                                    # dependsOn phases — read this before acting,
                                                    # it saves re-deriving context from raw events
```
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
- **`MODE == git`** (default — create a worktree):
  ```bash
  git -C "$REPO" fetch -q origin "$BASE"
  git -C "$REPO" worktree add "$MHOME/worktrees/$KEY" -b "${PREFIX}${KEY}" "origin/$BASE" 2>/dev/null \
    || git -C "$REPO" worktree add "$MHOME/worktrees/$KEY" "${PREFIX}${KEY}"   # adopt if branch exists
  # Prime it. `worktree add` brings tracked files only, so a fresh worktree silently lacks
  # gitignored repo guidance and any installed dependency tree. Mirror both from the source
  # checkout; each step is a no-op when the source doesn't have it.
  WT="$MHOME/worktrees/$KEY"
  if [ -f "$REPO/CLAUDE.local.md" ]; then cp "$REPO/CLAUDE.local.md" "$WT/"; fi
  if [ -d "$REPO/node_modules" ] && [ ! -e "$WT/node_modules" ]; then
    ln -s "$REPO/node_modules" "$WT/node_modules"
  fi
  maestro set-phase "$KEY" implementing --reason "worktree ready"
  ```
  The `node_modules` symlink is only sound while the ticket leaves dependencies untouched. If
  your change edits `package.json` or a lockfile, delete the symlink and run a real install in
  the worktree first — otherwise you are testing against the base branch's dependency tree.

**Done when:** either you slept via `maestro requeue "$KEY" 300` (an unmet dependency — nothing
else runs this step), or you appended exactly one `set-phase` event (`researching`, or
`implementing` once local mode needs no setup or the git worktree/branch exists), and
`maestro release "$KEY"` has run.
