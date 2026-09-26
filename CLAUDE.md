# CLAUDE.md

Guidance for Claude Code working in this repo. Keep it accurate as the code changes —
`tests/test_docs_drift.py` fails `make test` if a module below goes missing or a
backticked `module.name` stops resolving.

## What this is

**maestro** — a per-ticket reconciler over append-only event streams; a project-agnostic,
concurrent successor to wave-based markdown orchestrators. Each ticket is a directory with
its own append-only event log (the sole source of truth) and a small folded snapshot
(disposable cache). A cheap, level-triggered dispatcher sweeps snapshots and fans out one
independent headless `claude -p` reconciler per *due* ticket (or an opencode / pi session,
per the ticket's `runner:`); each reconciler takes ONE idempotent step and exits.

The governing principle: **deterministic plumbing in Python, intelligence in Claude.** The
`maestro` package owns everything correctness-critical (fencing-gated log, atomic writes,
fold, idempotent step-ids, dispatcher, leases, projections, dead-letter). Agents mutate
state **only** through the `maestro` CLI, so they can never write a torn log or clobber a
file. Read `DESIGN.md` for the full rationale (including the AC-evidence gates summarized
below), `README.md` for the quickstart, and `DOGFOOD.md` for running maestro on itself.

## Build / test / run

- Python ≥ 3.11, **stdlib-only core** (no runtime deps — `tomllib`, `fcntl`, `dataclasses`,
  `argparse`; `tests/test_import_graph.py` enforces it). Optional extras: `dev` (pytest,
  pytest-xdist, hypothesis, ruff), `tui` (textual). Keep the core dependency-free.
- `make install` — editable install of `.[dev,tui]` + symlink `maestro` onto PATH.
- `make test` — full suite in parallel (~30s). **Run before finishing.**
  `make t F=tests/test_x.py K=expr` — targeted run, stops at first failure.
  `make test-serial` — one process (for pdb). `make lint` — ruff (pyflakes rules only).
- `make status` / `make doctor` — board state / fleet health.
- `make reconcile KEY=<KEY>` — run ONE reconcile step in the foreground, skipping the sweep.
- `make dry` — one dispatcher sweep, read-only preview (`would_mint` + `would_spawn`, no
  `TicketCreated` appended, no sessions launched).
- `maestro dispatch --key <KEY>` (repeatable / comma-separated) — a REAL sweep restricted
  to the named ticket(s): due-checking, throttling, claims and the spawn ledger all run.
  Use it to watch the sweep machinery for one ticket; use `make reconcile` to watch just
  the next step. `make diagram` regenerates `docs/state-machine.md` + `docs/dispatch-gates.md`.

## ⚠️ Safety — read before any `maestro` command

- **The dogfood home is `~/.maestro`**, which is where bare `maestro` resolves. The
  `Makefile` exports `MAESTRO_HOME=$(HOME)/.maestro` (`maestro env` prints resolved paths).
  There is no `~/.maestro/maestro-dev` (the old home — `maestro` happily initialises a
  phantom there and reports it healthy). If the home ever moves, grep the whole repo for
  the old path; `tests/test_maestro_task_skill.py` pins every documented home to the Makefile.
- **Never delete or move a home's `events/`, `tickets/`, `inbox/` or `config.toml`** — not
  with `rm -rf`, `git clean`, or anything else, not even the dogfood board. The event logs
  have no other copy; this is how the board was lost on 2026-07-18. A genuine reset needs
  `maestro backup` first and the human's explicit, in-the-moment go-ahead.
- **Backups:** the dispatcher auto-snapshots those four paths every `backup_interval`
  (default 3600s) into the sibling `<home>-backups/` (keeps `backup_retention`, default 24).
  Verbs: `maestro backup [--list]`, `maestro restore [<tarball>] [--force]` — restore
  refuses to clobber a non-empty board without `--force`. Logic in `maestro/backup.py`;
  keep `tests/test_backup.py` green if you touch it.
- **Tests run on a `tmp_path` home, never a real one** (fixtures in `tests/conftest.py`).

## Architecture

`maestro/` — each module starts with a one-line docstring stating its single
responsibility; preserve that.

Core (correctness-critical):
- `store.py` — filesystem primitives: home resolution, atomic writes, per-key locks, paths.
- `event_log.py` — the append-only, **fencing-gated** event log (the sole truth).
- `events.py` — event type vocabulary. `snapshot.py` — fold of one ticket's log → snapshot.
- `statemachine.py` — the per-ticket phase machine (`Phase`, `TRANSITIONS`).
- `idempotency.py` — deterministic `step_id = hash(key, phase, observed_seq, action)`.
- `claims.py` — per-key liveness dedup backed by verified process identity.
- `inbox.py` — per-key append-only human inbox.
- `dispatcher.py` — level-triggered work queue: mint → find due → gate → spawn → exit.
  Also owns per-phase tool grants, VCS/CI sync, dispatcher-run test suites, runner routing.
- `ops.py` — high-level reconciler verbs (each correct-by-construction); what agents call.
- `cli.py` — argparse entrypoint; each subcommand is a `cmd_*` wrapper over `ops`.
- `sessions.py` — spawn/list reconciler sessions (`claude -p`, opencode, pi backends).
- `config.py` — project-agnostic `config.toml` loading/validation. Every `[maestro]` key
  is one row of `config.KNOBS` (coercion + whether `[repos.<name>]` may override it);
  unknown keys fail `config.load()` closed. Adding a knob: a `Config` field, a `KNOBS`
  row, a commented line in `DEFAULT_CONFIG_TOML` (`tests/test_config.py` checks all three).
- `repos.py` — per-ticket repo binding (`[repos.<name>]`). `gates.py` — spec front-matter reads.

Guards and budgets:
- `health.py` — `maestro doctor` checks. `alarm.py` — fleet-wide detection alarm.
- `burn.py` — per-key burn detection. `spend.py` — daily USD meter + ceiling gate.
- `ratelimit.py` — account-wide spawn gate from `rate_limit_event`s.
- `backup.py` — tarball snapshots/restore. `credentials.py` — per-repo `gh` credentials.
- `runner_permissions.py` / `pi_guard.py` — destructive-command guard for non-Claude runners.

Supporting:
- `context.py` — fold of a ticket's log into a context dossier. `locate.py` — file/symbol hints.
- `steplog.py` — reads session logs (Claude/opencode/pi) into steps.
- `projection.py` — snapshots → `derived/*.md` dashboards. `notify.py` — push notifications.
- `schedule.py` — interval/cron scheduled tasks. `decision_labels.py` — answer → route fold.
- `testlang.py` — per-language test-name extraction and selector formatting.
- `skills_install.py` — `maestro install-commands` (payload in `maestro/_skill_commands/`).
- `fleet.py` — launchd LaunchAgent management + pause switch.
- `diagram.py` — generates the two derived docs from `statemachine.TRANSITIONS` and an AST
  walk of `dispatcher.py`; `tests/test_diagram.py` fails on drift. It pins literal
  `decisions[...]["outcome"] = "..."` assignments and a few exact source lines in
  `dispatcher.py` — keep them literal when refactoring.
- `providers/` — pluggable tracker / VCS / fetcher / model adapters (selected in config).
- `tui/` — Textual TUI (`maestro tui`, `tui` extra). Import only via `maestro.tui`; new
  screens go in `screens.py`, new modals in `modals.py`.

Home layout (under `MAESTRO_HOME`): `tickets/<KEY>/spec.md` (human-owned),
`events/<KEY>.jsonl` (+ `.archive.jsonl`), `inbox/<KEY>.jsonl`, `derived/snapshots/`,
`derived/cursors/`, `derived/*.md` dashboards, `agent-logs/<KEY>/`, `tickets/_deadletter/`.

## Write-ownership rules (do not violate)

- **Humans** edit only `tickets/<KEY>/spec.md` and append to inboxes (`maestro ans`).
- **Agents** append only to `events/<KEY>.jsonl` (fencing-gated) and atomically replace
  `derived/*`. Never hand-edit an event log or a snapshot; go through the CLI / `ops`.
- `derived/WORKSTATE.md` and `derived/NEEDS-YOU.md` are generated — never edit them.

## QA convention: prove it with the real app

A change isn't done until a test exercises the real surface end-to-end:
- Invoke the real CLI (`cli.main([...])`) over a temp `MAESTRO_HOME` and assert the
  resulting events / snapshot / projection / exit code. For a flow, run a real sweep
  (`dispatch(cfg, DryRunSessions(), ...)`). Pass `--no-nudge` to human verbs in tests, or
  they spawn a live reconciler.
- Mock ONLY the genuinely external boundary — the `claude -p` spawn (`DryRunSessions`),
  network, `launchctl` — never the component under test.
- If you touch the log, dispatcher, claims or fold, add/adjust a test proving the
  invariant (idempotency, fencing, crash safety, single-writer) still holds.
- **TUI:** mount the real `MaestroTUI` in `tests/test_tui_runtime.py` with
  `async with app.run_test() as pilot:`, drive real keys, assert `app._exception is None`.
  Mocking `query_one` / `push_screen` / `notify` does not count. New bindings, screens and
  modals must be covered by the binding sweep and `test_every_binding_action_resolves`.
- Shared test helpers live in `tests/conftest.py` (`seed_ticket`, `seed_phase`,
  `make_origin_and_repo`, `git`, `run_doctor`, …) — reuse them instead of re-copying.

## Reconciler skills

Per-phase behavior lives in `.claude/commands/maestro-reconcile-<phase>.md`. `skills/` and
`maestro/_skill_commands/` are symlinks to those files — edit `.claude/commands/` only.
The dispatcher picks the command per key at spawn time
(`dispatcher.resolve_reconcile_command`). Agents drive state exclusively via `maestro` verbs.

## Ticket specs

Front-matter `priority` / optional `dependsOn`, then `## Intent`, `## Notes`,
`## Acceptance criteria` as `- [ ]` checkboxes — match existing tickets, don't invent
fields. An inert `approval_tier:` line in old specs is tolerated; don't bulk-rewrite it.
An AC line may end with `(test: <path>[::<id>])` or `(check: <shell command>)` to make it
machine-checked (see DESIGN.md, "Acceptance criteria").

## Gates that refuse on purpose

Each is default-on, pinned by the named tests. Rationale lives in DESIGN.md and docstrings.

| Rule | Where | Tests |
|---|---|---|
| A recorded QA verdict requires folded phase `qa` (`qa_phase_gate`) | `ops.record_qa_verdict` | `test_qa_phase_gate.py` |
| `set-phase awaiting-ci` requires a PASSING QA verdict on every current AC (`awaiting_ci_qa_gate`, not `--force`-able) | `ops._refuse_if_qa_incomplete` | `test_qa_phase_gate.py`, `test_qa_exit_gate_scope.py` |
| An annotated AC needs a current-tree passing capture, not a `verify-ac` attestation; `test:` must be ADDED by the diff | `ops.run_ac_checks`, `ops._acs_unverified_count` | `test_dispatcher_ac_checks.py`, `test_dispatcher_ac_checks_lang.py` |
| A net test deletion in `verifying` routes to `awaiting-human` (`test_deletion_gate`) | `dispatcher._route_test_run` | `test_dispatcher_ac_checks_lang.py` (`test_h4_*`) |
| `gh pr merge` is denied to every reconciler | `dispatcher.MERGE_DENYLIST` | `test_dispatcher.py`, `test_sessions.py` |
| PR is undrafted only once CI passes, no `CHANGES_REQUESTED`, every AC QA-passed | `dispatcher._maybe_undraft` | `test_undraft.py` |
| Unknown `language` / malformed `test_selector` fail `config.load()` closed | `config.load` | `test_repos.py`, `test_testlang.py` |
| `no_output_timeout` must cover `bash_max_timeout` (exported as `BASH_MAX_TIMEOUT_MS`) | `config.load` | `test_dispatcher.py` |

## Git

- Default branch `main`; reconcilers branch with prefix `maestro/` and open PRs per the
  ticket's repo binding (`maestro env --key <KEY>`; single-repo homes default to
  `cortop/maestro`). Don't use the `maestro/` prefix for hand-made branches.
- Commit/push only when asked. Branch first if on `main`.
