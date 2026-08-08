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
  # gitignored repo guidance and any installed dependency tree. Exclude the primed names
  # first -- `--git-path` resolves info/exclude to the *shared* common dir even for a linked
  # worktree, so the append must be idempotent -- then mirror each from the source checkout.
  # Every step is a no-op when the source doesn't have it.
  WT="$MHOME/worktrees/$KEY"
  EXC="$(git -C "$WT" rev-parse --git-path info/exclude)"
  for name in "CLAUDE.local.md" ".claude/settings.local.json" "node_modules/"; do
    grep -qxF "$name" "$EXC" || cat >> "$EXC" <<< "$name"
  done
  if [ -f "$REPO/CLAUDE.local.md" ]; then
    cp "$REPO/CLAUDE.local.md" "$WT/CLAUDE.local.md"
  fi
  if [ -f "$REPO/.claude/settings.local.json" ]; then
    mkdir -p "$WT/.claude"
    cp "$REPO/.claude/settings.local.json" "$WT/.claude/settings.local.json"
  fi
  if [ -d "$REPO/node_modules" ] && [ ! -e "$WT/node_modules" ]; then
    # cp -c is an APFS copy-on-write clone (write-isolated, near-instant); --reflink=auto is
    # its GNU/Linux equivalent (CoW on btrfs/xfs, and coreutils falls back to a normal deep
    # copy itself when the filesystem doesn't support it); cp -R is the last-resort deep copy
    # for any other cp. Deliberately no hardlink rung: a hardlink shares one inode, so a plain
    # in-place write (the common case, not just an atomic rename-into-place) mutates both the
    # worktree's and the source checkout's copies at once -- not write-isolated. The trailing
    # "/." copies node_modules' *contents* into place instead of nesting a second node_modules
    # inside it, so a rung that half-ran before failing still leaves a correct tree for the
    # next rung to finish.
    cp -c -R "$REPO/node_modules/." "$WT/node_modules" 2>/dev/null \
      || cp --reflink=auto -R "$REPO/node_modules/." "$WT/node_modules" 2>/dev/null \
      || cp -R "$REPO/node_modules/." "$WT/node_modules"
  fi
  maestro set-phase "$KEY" implementing --reason "worktree ready"
  ```
  The primed `node_modules` is a real, isolated copy, not a shared symlink -- an install run
  inside this worktree never writes through into `$REPO` or a sibling worktree. It does mean
  a ticket that edits `package.json` or a lockfile is priming against a tree that predates its
  own change; re-run the real install inside the worktree first in that case.

**Done when:** either you slept via `maestro requeue "$KEY" 300` (an unmet dependency — nothing
else runs this step), or you appended exactly one `set-phase` event (`researching`, or
`implementing` once local mode needs no setup or the git worktree/branch exists), and
`maestro release "$KEY"` has run.
