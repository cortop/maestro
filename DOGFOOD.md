# Dogfooding: maestro develops maestro

maestro is set up to orchestrate its own development. Tickets describing maestro's next
features live as specs; reconciler sessions implement them in isolated git worktrees and open
PRs against the ticket's bound repo (see below) for you to review and merge.

## The setup (already done)

| Piece | Where |
|-------|-------|
| `maestro` CLI | `~/.local/bin/maestro` → `.venv/bin/maestro` (editable install) |
| State home | `~/.maestro/maestro-dev/` (outside the repo, so worktrees don't nest) |
| Config | `~/.maestro/maestro-dev/config.toml` — `repo_path` = this repo, `vcs = github_cli` (`cortop/maestro`) |
| Reconcile command | `.claude/commands/maestro-reconcile-<phase>.md`, one per phase (tracked → every worktree inherits them) |
| Permissions | `.claude/settings.json` — allowlist so unattended reconcilers don't stall |
| Seed backlog | `M-1` (fleet CLI), `M-2` (log compaction), `M-3` (dependsOn gating) |

`maestro env` prints the home-wide resolved paths; `maestro env --key <KEY>` prints the repo a
*specific ticket* builds in (REPO/SLUG/BASE/PREFIX) — see "Multiple repos" below.

Single-repo homes (the out-of-box default, and everything above this line describes) need no
`[repos.*]` config at all: with no binding, `env --key` falls back to the legacy
`repo_path`/`branch_prefix` fields, so every existing home reconciles unchanged.

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
maestro answer                 # interactive walkthrough of all open questions
maestro answer M-1             # or scope to one ticket
maestro ans M-1 "yes — go ahead"  # non-interactive fallback
cat ~/.maestro/maestro-dev/derived/NEEDS-YOU.md

# then let it run on its own:
make dispatch               # one real sweep (spawns reconcilers for all due tickets)
make loop                   # or: sweep every 5 min in the foreground
maestro fleet up            # or: launchd-pinned, survives reboot (see daemon/README.md)
maestro fleet status        # loaded? heartbeat age? interval?
```

## Add your own work

```bash
maestro create              # interactive: title → tier → priority → $EDITOR opens spec template
maestro create "Short title" --tier 1 --intent "What done looks like + AC."  # non-interactive
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
ls ~/.maestro/maestro-dev/worktrees/   # per-ticket worktrees (always under the home, even
                                        # for tickets bound to a different [repos.*] entry)
```

## Safety notes

- Reconcilers run with `permission_mode = acceptEdits` + the `.claude/settings.json`
  allowlist. For fully unattended runs set `permission_mode = "bypassPermissions"` in
  `config.toml` (more autonomous, less guarded — your call).
- The dispatcher uses the **installed** maestro, which (editable) reflects `main`. A ticket
  that changes engine code only takes effect after its PR merges — so maestro improving
  itself is gated by your review, by construction.
- Start with `max_concurrency = 4`; raise it in `config.toml` once you trust the loop.

## Multiple repos

One `MAESTRO_HOME` can drive reconcilers across several repos: add a `[repos.<name>]` table
per repo (`path`, `slug`, optionally `base_branch`/`branch_prefix`/`default`) and bind a ticket
to one with `maestro create --repo <name>` (or a `repo:` line in its spec frontmatter). See the
commented example block in `config.toml`. A ticket with no binding keeps using the legacy
`repo_path`/`branch_prefix` fields — single-repo homes need no `[repos.*]` config at all.

**Activation checklist** — binding a *second* real repo to a live board is a human config step,
permitted only once MR-1 through MR-6 have all merged to `main`:

1. Install the `.claude/commands/maestro-reconcile-*.md` files — every bound repo needs them,
   since the reconciler's cwd becomes that repo (via `maestro env --key`) and each
   `/maestro-reconcile-<phase>` command (the dispatcher routes to the one matching the ticket's
   current phase — see `dispatcher.resolve_reconcile_command`) only resolves from a checkout
   that has it under `.claude/commands/`, or from the user commands directory (resolves from any
   cwd). `maestro install-commands --repo <name>` copies the six files into that
   `[repos.<name>]` checkout — prefer this when you own the repo and can commit them into it.
   `maestro install-commands --user` symlinks them into the user commands directory instead —
   prefer this for a repo the board does not own (e.g. a shared monorepo), since it leaves that
   repo's working tree untouched. Both are idempotent — safe to re-run after every skill edit.
2. Add a `[repos.<name>]` table for it to `config.toml` (`path` + `slug` at minimum).
3. Add its `owner/repo` slug to the VCS provider's repo list (`[vcs.github_cli] repos = [...]`)
   so `sync_vcs` polls PRs there too.
4. Grant the reconciler's permission surface in the new repo — without it, a spawned reconciler
   prints "This command needs your approval to run." and stalls with nobody there to approve it
   (the dispatcher never observes a session's exit status, so this looks like a healthy spawn
   until the no-progress watchdog eventually reacts, ~20 spawns and two hours later, and blames
   the reconciler logic rather than permissions). Either write a `permissions.allow` list in the
   new repo's `.claude/settings.json` (or `.claude/settings.local.json`, or your user-scope
   `~/.claude/settings.json`) covering `Bash(maestro:*)`, `Bash(git:*)`, `Bash(gh:*)`,
   `Bash(python3:*)`, `Bash(.venv/bin/:*)` — this repo's own `.claude/settings.json` is a working
   example — or set `permission_mode = "bypassPermissions"` in `config.toml` (the escape hatch
   above) to skip the settings-file check for the whole home. Verify with `maestro doctor` (the
   `reconciler_permissions` check names any repo and pattern still missing; `maestro doctor
   --strict` exits 1 while any check, including this one, isn't `ok`).
5. If the new repo is owned by a *different* `gh` account than the one already bound elsewhere on
   this board, bind its credential explicitly: `gh_account = "<login>"` (resolved via `gh auth
   token --user <login>` at spawn/poll time) or `token_env = "GH_TOKEN_..."` (read from that env
   var — deterministic under launchd, since a LaunchAgent does not inherit a login shell's
   env — see `maestro/fleet.py`; `token_env` wins when both are set) in the same `[repos.<name>]`
   table. **Until every repo bound by an in-flight ticket is bound to a credential the dispatcher
   can resolve (or is reachable by the single active `gh` account when no credential is
   configured), do not bind that repo to a ticket** — this is the corrected constraint (an
   earlier draft of this doc said "safe at `max_concurrency = 1`"; that's wrong, since the
   dispatcher's own `sync_vcs`/spawn credential resolution runs regardless of concurrency and
   fails closed with no ambient fallback — see `maestro/credentials.py`). The overlay covers `gh`
   API calls only (PR/CI/review polling, spawn env) — it does **not** cover `git push`, which
   still goes over whatever `git_protocol`/credential helper (ssh key, `osxkeychain`, ...)
   `~/.config/gh/hosts.yml` and your git config already resolve; threading a per-repo SSH
   identity is out of scope here. Verify with `maestro doctor` (the `gh_credential_reachability`
   check WARNs, per `[repos.*]` table with a `slug`, when the resolved credential can't see that
   repo).
6. Confirm MR-1 .. MR-6 are all merged (per-repo dispatcher plumbing, repo-scoped VCS, and this
   ticket's hardcode removal all need to be in place first).

## Backups (the event logs are the sole source of truth — protect them)

The dispatcher auto-snapshots the irreplaceable state (`events/` + `tickets/` + `inbox/` +
`config.toml`) on a timer — `backup_interval = 3600` by default (0 disables). Snapshots land
in a **sibling** of the home (`~/.maestro/maestro-dev-backups/` by default, overridable with
`backup_dir`), so a `rm -rf` of the home leaves them intact. Only the most-recent
`backup_retention` (24) tarballs are kept.

```bash
maestro backup            # snapshot now
maestro backup --list     # where they live + what exists
maestro restore           # restore the latest into the home (refolds snapshots + dashboards)
maestro restore <tarball> --force   # a specific one; --force overwrites a non-empty board
```

`restore` refuses to clobber a non-empty `events/`/`tickets/` unless `--force`, so a mistaken
restore can't wipe a live board.

## Pruning session logs (bounding `agent-logs/` disk usage)

The dispatcher auto-prunes stale reconciler session logs (`agent-logs/<KEY>/*.log` /
`*.stream.jsonl`) on a timer — `prune_interval = 3600` by default (0 disables). Retention is
`session_log_retention_days` (14) + `session_log_max_per_ticket` (200); either can be set to
`0` (or `None`) for "unlimited" on that dimension. A currently-live reconciler's own log is
never pruned. This is disk hygiene only — `agent-logs/` is deliberately excluded from `backup`
(see the module docstring in `backup.py`), since it's disposable transcript, not source of truth.

```bash
maestro prune-logs --all --dry-run   # preview: per-key file/byte counts, deletes nothing
maestro prune-logs --all             # prune every key
maestro prune-logs <KEY>             # prune just one key
```

One-off cleanup of a backlog (e.g. after a runaway spawn incident): back up first — the backup
protects `events/`, not the logs themselves, which are disposable by design — then dry-run to
see what would go, then actually prune:

```bash
maestro backup
maestro prune-logs --all --dry-run
maestro prune-logs --all
```
