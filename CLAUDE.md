# CLAUDE.md

Guidance for Claude Code working in this repo. Keep it accurate as the code changes.

## What this is

**maestro** — a per-ticket reconciler over append-only event streams; a project-agnostic,
concurrent successor to wave-based markdown orchestrators. Each ticket is a directory with
its own append-only event log (the sole source of truth) and a small folded snapshot
(disposable cache). A cheap, level-triggered dispatcher sweeps snapshots and fans out one
independent `claude --bg` reconciler per *due* ticket; each reconciler takes ONE idempotent
step and exits.

The governing principle: **deterministic plumbing in Python, intelligence in Claude.** The
`maestro` package owns everything correctness-critical (fencing-gated log, atomic writes,
fold, idempotent step-ids, dispatcher, leases, projections, dead-letter). Agents mutate
state **only** through the `maestro` CLI, so they can never write a torn log or clobber a
file. Read `DESIGN.md` for the full rationale, `README.md` for the quickstart.

## Build / test / run

- Python ≥ 3.11, **stdlib-only core** (no runtime deps — `tomllib`, `fcntl`, `dataclasses`,
  `argparse`). Optional extras: `dev` (pytest), `tui` (textual). Keep the core dependency-free.
- `make install` — editable install + symlink `maestro` onto PATH.
- `make test` — run the suite (`.venv/bin/python -m pytest -q`). Run this before finishing.
- `make status` / `make doctor` — board state / fleet health.
- `make reconcile KEY=<KEY>` — run ONE reconcile in the foreground (best way to watch a step).
- `make dry` — one dispatcher sweep, read-only preview (`would_mint` + `would_spawn`, no
  `TicketCreated` appended, no sessions launched).
- `maestro dispatch --key <KEY>` (repeatable, or comma-separated) — a REAL sweep restricted to
  just the named ticket(s): due-checking, throttling, claims, and the spawn ledger all run
  normally but only ever consider that candidate set, so it also exercises minting-adjacent
  machinery `make reconcile` skips entirely. Composes with `--dry-run`/`--model`. A throttled
  target idles instead of the normal sweep's slot-substitution (spawning a different due key);
  nothing is minted from the `_new` inbox on a `--key` sweep; an unknown key is a clear error.
  Use this over `make reconcile` when you need to watch due-checking/throttling itself for one
  ticket, not just its next reconcile step — `make reconcile` skips straight to invoking the
  reconcile command on an already-due ticket, bypassing the sweep machinery altogether.

## ⚠️ MAESTRO_HOME (read this before any `maestro` command)

Bare `maestro` resolves home to `~/.maestro`. **The dogfood home this project drives is
`~/.maestro/maestro-dev`.** The `Makefile` exports `MAESTRO_HOME=$(HOME)/.maestro/maestro-dev`,
so `make` targets are correct — but a raw `maestro <cmd>` in the shell will hit the wrong home.
Always `export MAESTRO_HOME=~/.maestro/maestro-dev` (or use a `make` target) when inspecting or
mutating the self-dev board. `maestro env` prints the resolved paths. See `DOGFOOD.md`.

## ⚠️ NEVER delete the state home / event logs

**Do not run `rm -rf` (or any delete/move/`git clean`) against a MAESTRO_HOME, its `events/`,
`tickets/`, `inbox/`, or `config.toml` — not the dogfood/dev home (`~/.maestro/maestro-dev`),
not any home.** The event logs are the sole source of truth and have no other copy; deleting them
is unrecoverable. "It's only the dev/dogfood board" is **not** a reason to delete it — that board
is real work-in-progress (this is exactly how it was lost on 2026-07-18). If a home genuinely
needs resetting, `maestro backup` first, then delete only with the human's explicit, in-the-moment
go-ahead. Tests must operate on a `tmp_path`, never on a real home (see `tests/conftest.py`).

## ⚠️ Backups — the event logs are the sole source of truth

There is **no other copy** of a ticket's history. An external `rm` (a stray shell command, a
`--dangerously-skip-permissions` agent) that deletes `events/` is unrecoverable — this is exactly
how the dogfood board was lost once (2026-07-18). Guard against it:

- The dispatcher **auto-snapshots** `events/` + `tickets/` + `inbox/` + `config.toml` on a timer
  (`backup_interval`, default 3600s; `0` disables). Snapshots are `.tar.gz` tarballs in a
  **sibling** of the home (`backup_dir`, default `<home>-backups/`), so they survive a
  `rm -rf <home>`. Only the most-recent `backup_retention` (default 24) are kept. Logic lives in
  `maestro/backup.py` (stdlib-only: `tarfile` + `datetime`); it is wired into `dispatch()` via
  `backup.maybe_backup(cfg, now)`, cursor-gated exactly like `sync_external_sources`.
- Verbs: `maestro backup` (snapshot now), `maestro backup --list`, `maestro restore [<tarball>]`
  (default: latest; refolds snapshots + dashboards after extracting). `make backup` / `make restore`
  (`FORCE=1` to overwrite). `restore` refuses to clobber a non-empty `events/`/`tickets/` without
  `--force`, so a mistaken restore can't wipe a live board.
- If you touch `backup.py`, the dispatcher hook, or the config knobs, keep `tests/test_backup.py`
  green — it drives the real CLI over a temp home through a backup → wipe → restore round-trip.

## Architecture (package layout)

`maestro/` (the correctness-critical core):
- `cli.py` — argparse entrypoint; every subcommand is a `cmd_*` thin wrapper over `ops`.
- `store.py` — filesystem primitives: home resolution, atomic writes, advisory per-key locks.
- `event_log.py` — the append-only, **fencing-gated** event log (the heart; the sole truth).
- `events.py` — event type vocabulary. `snapshot.py` — fold of one ticket's log → snapshot.
- `dispatcher.py` — level-triggered, non-blocking work queue: mint → find due → spawn → exit.
- `sessions.py` — spawn/list per-ticket reconciler `claude --bg` sessions.
- `ops.py` — high-level reconciler verbs (each correct-by-construction); what agents call.
- `statemachine.py` — the per-ticket phase machine. `claims.py` — per-key liveness dedup.
- `idempotency.py` — deterministic `step_id = hash(key, phase, observed_seq, action)`.
- `inbox.py` — per-key append-only human inbox. `projection.py` — snapshots → dashboards.
- `config.py` — project-agnostic config (`config.toml`). `fleet.py` — launchd LaunchAgent mgmt.
- `diagram.py` — generates `docs/state-machine.md` (mermaid) + `docs/dispatch-gates.md`
  (`make diagram`) from `statemachine.TRANSITIONS` and an AST walk of `dispatcher.py` —
  derived, never retyped; `tests/test_diagram.py` fails `make test` on drift.
- `providers/` — pluggable tracker / VCS / fetcher / implementer (selected in config).
- `tui/` — Textual TUI package (`maestro tui`, needs the `tui` extra): `app.py` (MaestroTUI +
  entrypoint), `screens.py` (full-screen views), `modals.py` (input dialogs), `render.py`
  (markup helpers), `detail.py`/`events.py` (textual-free renderers). External code imports
  only via `maestro.tui` (`__init__.py` re-exports); new screens go in `screens.py`, new
  modals in `modals.py`.

Home directory layout (under `MAESTRO_HOME`): `tickets/<KEY>/spec.md` (human-owned),
`events/<KEY>.jsonl` (+ `.archive.jsonl`), `inbox/<KEY>.jsonl`, `derived/snapshots/`,
`derived/cursors/`, `derived/*.md` dashboards, `agent-logs/<KEY>/`, `tickets/_deadletter/`.

## Write-ownership rules (do not violate)

- **Humans** edit only `tickets/<KEY>/spec.md` and append to inboxes (`maestro ans`).
- **Agents** append only to `events/<KEY>.jsonl` (fencing-gated) and atomically replace
  `derived/*`. Never hand-edit an event log or a snapshot; go through the CLI / `ops`.
- `derived/WORKSTATE.md` and `derived/NEEDS-YOU.md` are generated projections — never edit them.

## Conventions

- Each module starts with a one-line docstring stating its single responsibility — preserve that.
- **QA proves the feature with the real app — every change, not just tests that pass.** A change
  isn't done until a test exercises the actual surface a human or agent touches and demonstrates
  the expected behavior end-to-end: invoke the real CLI (`cli.main([...])` or the `maestro` verb)
  over a temp `MAESTRO_HOME` and assert the resulting events / snapshot / projection / exit code;
  for a flow, run a real dispatcher sweep (`dispatch(cfg, DryRunSessions(), ...)`); for the TUI,
  mount the real app (next bullet). Mock ONLY the genuinely external boundary — the `claude -p`
  spawn (`DryRunSessions`), network, `launchctl` — never the component under test. `make reconcile
  KEY=…` runs a real reconcile step in the foreground; `make dry` runs a real sweep the same way
  but strictly read-only (`would_mint`/`would_spawn`, no writes) — good for watching what a sweep
  would do without minting or spawning anything.
- Correctness invariants (idempotency, fencing, crash safety, single-writer) are all tested in
  `tests/`. If you touch the log, dispatcher, claims, or fold, add/adjust a test proving the
  invariant still holds.
- **The TUI instance of that rule: mount the real app.** If you touch anything under
  `maestro/tui/`, prove it by mounting `MaestroTUI` through Textual: add/extend
  `tests/test_tui_runtime.py` with `async with app.run_test() as pilot:`, drive real keys
  (`await pilot.press(...)`), and assert `app._exception is None`. Mocking `query_one` /
  `push_screen` / `notify` does NOT count as QA — that is exactly what lets a forgotten widget id,
  an `on_mount` exception, or a binding to a missing action ship uncaught. Any new binding, screen,
  or modal must be covered by the binding sweep and `test_every_binding_action_resolves` (a missing
  action is a silent no-op at runtime, so the static check is what catches it). `make test` must
  stay green; the runtime tests need the `tui` extra, so install `.[dev,tui]`.
- Reconciler behavior lives in per-phase `/maestro-reconcile-<phase>` skills
  (`.claude/commands/maestro-reconcile-<phase>.md`, mirrored in `skills/`; tracked so every
  worktree inherits them) — progressive disclosure, so a reconciler only loads the branch its
  current phase needs. The dispatcher resolves which one to spawn per-key at spawn time
  (`dispatcher.resolve_reconcile_command`, beside `_resolve_model_effort`/`MERGE_DENYLIST`).
  Agents drive state exclusively via `maestro` verbs.
- Ticket specs follow a fixed format (front-matter `priority` / optional `dependsOn`, then
  `## Intent`, `## Notes`, `## Acceptance criteria` as `- [ ]` checkboxes) — match existing
  tickets, don't invent fields. Some existing specs still carry a now-inert `approval_tier:`
  line (AD-7 removed the tier gate it drove) — tolerated as an unrecognized front-matter key,
  never rewritten in bulk.

## Git

- Default branch `main`; reconcilers branch with prefix `maestro/` and open PRs per the ticket's
  repo binding (`maestro env --key <KEY>`; single-repo homes default to `cortop/maestro`).
- Commit/push only when asked. Branch first if on `main`.
