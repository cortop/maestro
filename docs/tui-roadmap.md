# maestro TUI — feature roadmap

A textual TUI that covers **every human-facing maestro capability**, sliced into
one independently-shippable feature per ticket. Each section below maps 1:1 to a
maestro ticket: copy its **Goal** + **Acceptance** into the spec when you
`maestro create`.

Design rules that apply to every ticket:

- **Core stays stdlib-only.** `textual` lives in the `tui` extra. The `maestro
  tui` entrypoint imports textual *lazily* (inside the function) so the plain CLI
  never imports it.
- **The TUI is a thin shell over existing APIs.** It never reimplements logic —
  it reads `snapshot`/`event_log`/`inbox`/`fleet` and writes only through the
  human channels (`inbox.append_command`, `inbox.append_new`, spec edits). No new
  state, no new write paths.
- **One reconcile = one phase.** The TUI only *observes* state and *appends* to
  inboxes; phase transitions remain the reconciler's job. The TUI never calls the
  agent verbs (`set-phase`, `fail`, `finalize`, …).
- **Home discipline.** Resolve home via `config.load(home)` honoring
  `$MAESTRO_HOME`; surface the resolved home in the UI so the dogfood-vs-`~/.maestro`
  gotcha is visible.

Dependency order: **T1 → T2 → T3** are the spine; T4–T13 each build on T3 and are
otherwise independent (parallelizable).

---

## T1 — App skeleton + `maestro tui` entrypoint

**Goal:** A runnable Textual app with header/footer/quit, plus a `maestro tui`
subcommand that launches it with a clear error if the extra isn't installed.

**maestro surface:** new entrypoint; `env`/`config.load` for home resolution.

**Notes:**
- `maestro/tui.py` with `MaestroTUI(App)` and `main()`.
- `cmd_tui` in `cli.py` (registered as `add("tui", cmd_tui, ...)`) that does a
  lazy `from .tui import main`; on `ImportError` print
  `install the TUI extra: pip install -e '.[tui]'` and return 2.
- App holds `self.cfg = config.load(args.home)`; pass `--home` through.
- Footer shows the resolved home path.

**Acceptance:**
- `maestro tui` opens a window with header, footer, and `q` to quit.
- With textual uninstalled, `maestro tui` exits 2 with the install hint (core CLI
  still imports and runs).
- Resolved home is visible in the UI and respects `$MAESTRO_HOME` / `--home`.

---

## T2 — Live ticket table (`status`)

**Goal:** A `DataTable` of all tickets (key, phase, title, PR, CI, tier, fails)
that refreshes on a timer and on demand.

**maestro surface:** `status`; mirrors `projection.render` WORKSTATE columns.

**Notes:**
- Rows from `dispatcher.list_keys(home)` + `snapshot.load(home, key)`.
- `add_row(..., key=s.key)` so later features resolve the selected ticket.
- `set_interval(3.0, reload)` + an `r` binding. Sort by the WORKSTATE phase order.

**Acceptance:**
- Table lists every active ticket with correct phase/PR/CI/fails.
- New tickets / phase changes appear within one refresh interval; `r` forces it.
- Empty home renders an empty table without crashing.

---

## T3 — Ticket detail pane (`show`)

**Goal:** A side pane that shows the highlighted ticket's full snapshot.

**maestro surface:** `show` (snapshot portion).

**Notes:**
- `Horizontal(DataTable, Static#detail)`; react to
  `DataTable.RowHighlighted` → `snapshot.load`.
- Render title, tier, source, PR url/state, CI, failure_count, last_error,
  open_questions, updated_ts. Use Rich markup for emphasis.

**Acceptance:**
- Arrowing through rows updates the detail pane to that ticket.
- All snapshot fields (incl. last_error and open_questions) render; missing
  values show `—`.

---

## T4 — Event timeline (`events` / `show`)

**Goal:** A scrollable, newest-last event log for the selected ticket.

**maestro surface:** `events`, `show` (events portion).

**Notes:**
- `event_log.read(home, key)`; a `RichLog`/`DataTable` of seq, ts, type, actor,
  payload summary. Toggle full vs tail (`--tail`-style). Optional second screen
  bound to `enter`.

**Acceptance:**
- Selecting a ticket shows its events in order; updates on refresh.
- Can view the full log and a tail; large logs scroll without lag.

---

## T5 — Answer open questions inline (`ans` / `answer`)

**Goal:** Press a key on a ticket with open questions → input modal → records the
answer; the triage loop without leaving the TUI.

**maestro surface:** `ans`, `answer` → `inbox.append_command(home, key, "ans", {"qid", "text"})`.

**Notes:**
- Bind `a`; if the ticket has multiple `open_questions`, walk them like
  `cmd_answer` does. Modal `Input`; on submit append and `reload`.
- Surface a confirmation toast; the answer is consumed on the next reconcile
  (don't expect instant phase change).

**Acceptance:**
- Answering a question appends the correct inbox command (verify with
  `inbox.pending`).
- Multi-question tickets are walked one at a time; cancel leaves state untouched.

---

## T6 — Create ticket form (`create`)

**Goal:** A modal form to queue a new ticket.

**maestro surface:** `create` → `inbox.append_new(home, title, key, {"approval_tier","priority","intent"})`.

**Notes:**
- Bind `n`; fields: title (required), key (optional), tier (default 1), priority
  (default 3), intent (optional). Append and toast "queued; dispatcher will mint
  the key".

**Acceptance:**
- Submitting appends a `_new.jsonl` entry with the right args (verify with
  `inbox.pending_new`).
- The new ticket appears in the table after the next dispatch sweep.

---

## T7 — Ticket command actions (`cmd`) — retry / discard / revive

**Goal:** An action menu to send arbitrary commands to a ticket, with first-class
revive/drop for `degraded`.

**maestro surface:** `cmd` → `inbox.append_command(home, key, command, {"text"})`.

**Notes:**
- Command palette or keys (`retry`, `discard`); free-text command + args option.
- For `degraded` rows expose one-key **retry** / **discard** mirroring the
  NEEDS-YOU.md hints.

**Acceptance:**
- Each action appends the matching inbox command.
- Degraded tickets offer retry/discard directly from the list.

---

## T8 — Filtered views / "Needs you" queue (`status`)

**Goal:** Toggle between **all / needs-you / active** filters; default a
needs-you queue (`awaiting-human` + `degraded`).

**maestro surface:** `status` needs_you; mirrors NEEDS-YOU.md.

**Notes:**
- Filter the table source by phase sets (reuse `SLEEPING/ACTIVE` phase sets from
  `statemachine`). Show counts per filter in the header.

**Acceptance:**
- Switching filters changes the visible rows; counts are correct.
- Needs-you view shows exactly the tickets `cmd_status` reports under `needs_you`.

---

## T9 — Fleet & health panel (`fleet` / `doctor` / `dispatch`)

**Goal:** A panel showing dispatcher state and letting you control it.

**maestro surface:** `fleet up/down/status`, `doctor`, `dispatch`, `project`.

**Notes:**
- Show `fleet.status(home)` (loaded, heartbeat age, interval, label) + `doctor`
  (dead-letters, stale). Keys to `fleet up`/`down` (with interval prompt) and to
  run a one-off `dispatch` (offer `--dry-run`) and `project`.
- Run blocking calls in a worker/thread so the UI stays responsive.

**Acceptance:**
- Panel reflects real launchd state and heartbeat age; refreshes live.
- Up/down toggles the LaunchAgent; a manual dispatch sweep runs and the table
  updates after.

---

## T10 — Spec viewer + pending inbox (`show` / spec edits)

**Goal:** View a ticket's `spec.md` and its pending (unconsumed) inbox commands;
open the spec in `$EDITOR`.

**maestro surface:** spec.md human channel; `show` pending_inbox via `inbox.pending`.

**Notes:**
- Read `tickets/<KEY>/spec.md`; render markdown. List `inbox.pending(home, key)`.
- Bind a key to suspend the app and launch `$EDITOR` on the spec (`App.suspend()`),
  then reload on return.

**Acceptance:**
- Spec renders for the selected ticket; pending commands are listed.
- Editing the spec via the TUI persists to `spec.md` and is picked up on the next
  reconcile (spec-hash change).

---

## T11 — Config / env viewer (`env`)

**Goal:** A read-only panel of resolved config.

**maestro surface:** `env` (`config.load`): home, repo_path, branch_prefix,
reconcile_command, max_concurrency, max_impl_turns, providers.

**Notes:** Plain key/value panel on a bound screen; optionally show the raw
`config.toml` path.

**Acceptance:** Panel shows the same values as `maestro env` for the active home.

---

## T12 — Maintenance ops (`compact` / `release` / `project`)

**Goal:** Per-ticket log compaction and claim release; global dashboard regen.

**maestro surface:** `compact`, `release`, `project`.

**Notes:**
- Per-ticket actions: `compact` (fold pre-snapshot events) and `release` (drop a
  stuck claim) — confirm before running. Global `project` regen button.
- Guard `release` with a confirmation since it affects claim state.

**Acceptance:**
- Compact reduces the on-disk event log for a ticket; the snapshot is unchanged.
- Release clears the ticket's claim; project regenerates WORKSTATE/NEEDS-YOU.

---

## T13 — Phase styling + live notifications (polish)

**Goal:** Visual phase encoding and toasts when tickets need attention.

**maestro surface:** cross-cutting (phases, open_questions, degraded).

**Notes:**
- Color/badge rows by phase (active vs sleeping vs degraded vs done).
- On refresh, toast when a ticket newly enters `awaiting-human` or `degraded`
  (diff against the previous poll).

**Acceptance:**
- Rows are visually distinguishable by phase.
- A ticket entering awaiting-human/degraded raises a notification once (not every
  tick).

---

## Coverage check

| maestro capability | Ticket |
|---|---|
| `status` | T2, T8 |
| `show` (snapshot) | T3 |
| `show` (events) / `events` | T4 |
| `ans` / `answer` | T5 |
| `create` | T6 |
| `cmd` | T7 |
| `fleet` / `doctor` / `dispatch` | T9 |
| spec.md edit / pending inbox | T10 |
| `env` | T11 |
| `compact` / `release` / `project` | T12 |
| entrypoint / home resolution | T1 |

**Intentionally out of scope:** `init` (one-time scaffold, better as a CLI/first-run
step) and the agent-only verbs (`append`, `set-phase`, `ask`, `fold-inbox`,
`inbox-ack`, `observe-spec`, `requeue`, `fail`, `finalize`) — those are the
reconciler's API, never a human surface.
