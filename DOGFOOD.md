# Dogfooding: maestro develops maestro

maestro is set up to orchestrate its own development. Tickets describing maestro's next
features live as specs; reconciler sessions implement them in isolated git worktrees and
open PRs against `cortop/maestro` for you to review and merge.

## The setup (already done)

| Piece | Where |
|-------|-------|
| `maestro` CLI | `~/.local/bin/maestro` → `.venv/bin/maestro` (editable install) |
| State home | `~/.maestro/maestro-dev/` (outside the repo, so worktrees don't nest) |
| Config | `~/.maestro/maestro-dev/config.toml` — `repo_path` = this repo, `vcs = github_cli` (`cortop/maestro`) |
| Reconcile command | `.claude/commands/maestro-reconcile.md` (tracked → every worktree inherits it) |
| Permissions | `.claude/settings.json` — allowlist so unattended reconcilers don't stall |
| Seed backlog | `M-1` (fleet CLI), `M-2` (log compaction), `M-3` (dependsOn gating) |

`maestro env` prints the resolved paths the reconciler uses.

## The loop

```
you write a spec  ─▶  dispatcher mints + spawns a reconciler  ─▶  it triages
      ▲                                                              │
      │                                                       tier≥1: asks you
   merge PR ◀── reconciler opens PR ◀── implements in worktree ◀── you answer
```

A reconciler takes **one step per run** and exits; the dispatcher re-wakes each ticket as
it becomes due. Many tickets advance concurrently and independently.

## Drive it

```bash
make status                 # where everything stands
make dry                    # safe: what the dispatcher WOULD spawn (no sessions launched)

# try ONE ticket in the foreground first (watch it think):
make reconcile KEY=M-1      # triages M-1 -> asks a pickup question

# answer questions it raises (NEEDS-YOU.md lists them):
maestro ans M-1 "yes — go ahead"
cat ~/.maestro/maestro-dev/derived/NEEDS-YOU.md

# then let it run on its own:
make dispatch               # one real sweep (spawns reconcilers for all due tickets)
make loop                   # or: sweep every 5 min in the foreground
make fleet-up               # or: launchd-pinned, survives reboot (see daemon/README.md)
```

## Add your own work

```bash
maestro create "Short title" --tier 1 --intent "What done looks like + AC."
# edit the richer spec by hand any time — it's yours, append-only-safe:
$EDITOR ~/.maestro/maestro-dev/tickets/<KEY>/spec.md
```

You can edit specs and answer questions **while reconcilers are running** — your writes go
to human-owned/append-only files, never the ones agents rewrite.

## Watch / debug

```bash
maestro show M-1            # snapshot + event log for one ticket
maestro doctor             # heartbeat age, dead-letters
claude agents --json       # live agent-view sessions
ls ~/.maestro/maestro-dev/worktrees/   # per-ticket worktrees
```

## Safety notes

- Reconcilers run with `permission_mode = acceptEdits` + the `.claude/settings.json`
  allowlist. For fully unattended runs set `permission_mode = "bypassPermissions"` in
  `config.toml` (more autonomous, less guarded — your call).
- The dispatcher uses the **installed** maestro, which (editable) reflects `main`. A ticket
  that changes engine code only takes effect after its PR merges — so maestro improving
  itself is gated by your review, by construction.
- Start with `max_concurrency = 4`; raise it in `config.toml` once you trust the loop.
