# The 2026-07-19 runaway: the control set and its gaps

**Date:** 2026-08-18 · **Status:** dated incident record, not drift-guarded prose · **Verdict:**
ten controls now bound the blast radius this incident had none against; two were added only
after the first round shipped, and this document exists because the reasoning for the other
eight was scattered across seven source files as inline comments, invisible as a set.

On 2026-07-19 the dispatcher spawned 21,731 no-op reconciler sessions over roughly 35 hours,
burning ~$845, before a human noticed. Nothing that existed at the time would have caught it:
`maestro doctor` read green throughout (fresh heartbeat, zero dead letters — see `health.py:5`),
because every existing check answered "is the dispatcher alive", and none of them answered "is it
doing too much". This document is that missing record: the timeline, the root causes, every
control that now exists because of this incident (plus the unrelated 2026-07-18 board wipe, whose
one control — backup/restore — is included here for the same reason), and the gaps that stayed
open after the first round shipped. It is the last item of the six-gap incident list; the other
five are closed.

Everything below marked with a file:line citation was read at HEAD on `main`, not reconstructed
from a changelog — see Posture at the end for what that means for this doc's own upkeep.

---

## 1. Timeline

Four measured quantities, all still visible in the historical event stream and the code comments
that cite them:

| quantity | value |
|---|---|
| time undetected | 35 hours |
| no-op sessions spawned | 21,731 |
| discarded `RequeueScheduled` events | 5,522 |
| total spend | ~$845, of which **~$658 was spent spawning into a rate-limit wall the fleet had
already hit** (`maestro/ratelimit.py:2-3`) |

The $658 figure is the single largest cost fact of the incident and, before this document,
appeared in no document at all — only in `ratelimit.py`'s own module docstring, written after
the fact to justify the module it introduces (§3, "account rate-limit gate").

## 2. Root causes

At least four distinct causes, each recorded today in a different source file, none of them
cross-referenced against the others until now:

1. **The requeue-timer test sat in the wrong `if` branch.** `dispatcher.is_due`'s pending-timer
   check used to live inside the `SLEEPING_PHASES` arm only, so a `maestro requeue` issued from an
   *active* phase — exactly what the in-review handler does — was ignored, and the ticket came
   back due on the very next sweep. `dispatcher.py:431` carries the fix and the incident numbers
   in the same comment: "the 2026-07-19 runaway, 21,731 no-op sessions with 5,522 discarded
   RequeueScheduled events behind them". The check now runs first, unconditionally, for every
   non-terminal phase (§3, GA-5's sibling gates aside — this fix predates them and is not itself
   one of the ten controls; it is the bug they all now backstop).
2. **No fleet-wide meter or ceiling existed in any unit.** `maestro/spend.py`'s own module
   docstring: "nothing in the package was denominated in money — `daily_token_ceiling` sat
   parsed-and-unread". The fleet had a rate concept (spawns) but no cost concept at all; a correct
   decision function running at high rate is still unbounded spend.
3. **Nothing pushed a signal toward a human.** `maestro/alarm.py:1-3`: "the 2026-07-19 runaway ran
   35 hours before anyone noticed because nothing in the package pushed toward a human — only
   `maestro doctor`'s `runaway` field would have caught it, and nobody was polling it." Detection
   existed in principle (a field in a JSON blob) and not in practice (nothing read that field
   without being asked).
4. **The pre-existing doctor checks all read green.** `maestro/health.py:1-5`: the module's own
   framing is "the existing liveness signals only answer 'is the dispatcher alive'; this answers
   'is it doing too much'" — precisely because at the time of the incident nothing answered the
   second question. A fresh heartbeat and zero dead letters is what a runaway board looks like
   from the outside, not just what a healthy one looks like.

These four are independent: fixing any one alone would not have closed the incident. #1 is the
trigger; #2-#4 are why nothing bounded, surfaced, or was even a place to look once #1 fired.

## 3. The control set

All ten below are real and wired into `dispatch()` at HEAD — every citation was read from the
actual source, not carried over from a prior draft. Nine ship today; the tenth (§4) does not, and
is documented as a gap rather than a control for that reason.

### GA-5 — runaway auto-pause

`dispatcher._maybe_trip_runaway_brake` (`maestro/dispatcher.py:2081`), armed inside `dispatch()`
once the rate-limit gate and daily-spend gate have both had first refusal
(`maestro/dispatcher.py:2496-2501`); state persisted at `derived/.runaway_brake.json`; config knob
`cfg.runaway_pause_cooldown` (default 900s, `maestro/config.py:111`). Tests:
`tests/test_runaway_brake.py`.

**Why this one:** the incident's decision function (`is_due`) was *correct given its inputs* —
the fleet did exactly what the code said. A proof about that function bounds nothing; only a
meter with a ceiling does. GA-5 is the meter denominated in the same unit the incident actually
ran at (spawns/hour, via `health.spawn_rate`/`health.spawn_budget`), and it self-arms — no human
has to be watching for it to trip. A prior-armed-pause resume race is guarded by the persisted
`until` marker: a human `resume` right after a pause could otherwise be undone the very next
sweep by a naive re-check re-arming on the spot; the cooldown is measured from the *last* armed
`until`, not from `now`, so a resume genuinely takes effect.

### GA-8 — spawn floor

`cfg.min_spawn_interval` (`maestro/config.py:28`), consulted by `dispatcher.spawn_floor`
(`maestro/dispatcher.py:2021`); `health.check_spawn_floor` (`maestro/health.py:844`) WARNs when
the floor reads 0. Tests: `tests/test_dispatcher.py`, `tests/test_health.py`.

**Why this one:** a hard per-key floor is independent of claim liveness — a session that dies in
under a second frees its claim instantly, so liveness-based throttling alone cannot bound
re-spawn rate. GA-8 bounds it regardless of how fast the dispatcher itself is invoked (the
2026-07-19 regime ran sweeps roughly every 11 seconds). The floor has no surface of its own
outside `config.toml`, so `check_spawn_floor` exists purely so a debugging override left at 0
doesn't silently disarm the one setting most directly named after this incident's shape (a tight
respawn loop).

### GA-11 — daily USD ceiling

`cfg.daily_spend_ceiling_usd` (`maestro/config.py:101`), folded from session logs by
`spend.probe` (`maestro/spend.py:75`) and gated by `spend.over_ceiling`
(`maestro/spend.py:222`), consulted in `dispatch()` right after the runaway brake
(`maestro/dispatcher.py:2506-2510`); `health.check_daily_spend` (`maestro/health.py:860`) surfaces
both the meter and an unset ceiling as a WARN (RB-8 — a hard cap armed in code and disarmed in
practice is exactly the class of finding this incident is about). Tests: `tests/test_spend.py`.

**Why this one:** GA-5 bounds *rate*; nothing before GA-11 bounded *cumulative* spend for a fleet
running legitimately, but expensively, all day. `spend.py`'s module docstring is explicit that
this is the fix for root cause #2 — cost, not rate, was the unit nothing existed in.

### GA-14 — sub-agent spawn weighting

`dispatcher.spawn_weight` (`maestro/dispatcher.py:2029`), `dispatcher._ledger_entry_weight`
(`maestro/dispatcher.py:2067`), `health._budget_weight` (`maestro/health.py:72`), unit
`health.SPAWN_RATE_UNIT` = `"agent-equivalents"` (`maestro/health.py:69`). Tests:
`tests/test_health.py`.

**Why this one, and why it shipped after the others:** see §4 — this is one of the two controls
that did not exist in the first hardening round because the gap it closes was itself invisible
until a later incident.

### RB-12 — alarm channel

`maestro/alarm.py`, fired from `dispatch()` right after the rate-limit/runaway/spend gates decide
the sweep (`maestro/dispatcher.py:2508-2512`), reusing `notify.py`'s existing transport
(`notify_command`/`webhook_urls`) via its own checked variant, `alarm._fire`
(`maestro/alarm.py:107`); dispatch entrypoint `alarm.check` (`maestro/alarm.py:159`); config table
`[alarm]` (`maestro/config.py:298`). Tests: `tests/test_alarm.py`.

**Why this one:** root cause #3. `alarm.py`'s own docstring draws the line precisely: unlike
`notify.maybe_notify` — an advisory phase-transition ping that silently swallows a broken
command/webhook — RB-12 is the fleet's *one* detection channel, so a broken transport must itself
be loud. `_fire` does not reuse `notify.py`'s swallow-everything helpers; it raises, and
`_run_hook` records the failure under `hook_errors["alarm"]` (surfaced in `maestro why`) rather
than letting a broken webhook silently mean nobody is told twice. Dedup state is written *before*
the notification is attempted, so a broken transport still durably records "this episode already
tried to fire" and doesn't retry-spam a subprocess call every sweep while it stays broken.

### Account rate-limit gate

`maestro/ratelimit.py`, probed on every real sweep — never on a cadence — at
`maestro/dispatcher.py:2497` (`ratelimit.paused_until`, `maestro/ratelimit.py:158`), consumed as
the first of the three spawn-nothing gates at `maestro/dispatcher.py:2496`. Underlying meter:
`ratelimit.probe` (`maestro/ratelimit.py:88`). Tests: `tests/test_ratelimit.py`.

**Why this one:** the single most expensive gap. $658 of the $845 was spent spawning sessions
that the API's own `rate_limit_event` records show were rejected on arrival — the fleet had
already hit a wall and kept paying to hit it again. This gate is checked first, ahead of the
human-signal bypass every other gate honors, on the principle that an inbox answer must not punch
through a 429 either, since that spawn would be rejected too.

### Turn budgets

Two independent layers, one self-reported and one ground-truth:

- **Self-reported:** `cfg.max_impl_turns` (`maestro/config.py:50`, default 20), recorded by
  `ops.record_impl_turn` (`maestro/ops.py:1637`) — a session that never calls `maestro impl-turn`,
  or calls it once, binds nothing.
- **Ground-truth:** `cfg.max_session_turns` (`maestro/config.py:64`), passed natively as
  `--max-turns` to the runner itself (`sessions.ClaudeCliSessions`, `maestro/sessions.py:227-233`)
  — enforced at a layer the agent cannot decline to honor, since it is the runner's own CLI flag,
  not a self-reported counter. `cfg.max_turn_wallclock_seconds` (`maestro/config.py:90`) is the
  dispatcher-side backstop inside `dispatcher.run_watchdog` (`maestro/dispatcher.py:1807`) for a
  runner with no native `--max-turns`-equivalent.

**Why both:** measured 2026-08-15, a session that called `impl-turn` exactly once had actually run
191 raw model turns and 61.1M input tokens against that one self-reported counter — roughly 95x
off. The self-reported counter still exists (it bounds the implementing↔qa fix-round ping-pong,
a different failure shape), but it cannot be the runaway backstop; only a cap the agent cannot
decline to honor can be.

### `detect_zero_turn_spawns`

`dispatcher.detect_zero_turn_spawns` (`maestro/dispatcher.py:1881`), run before the session
manager's `list_active()` sweep releases any stale claim — ordering that matters, since the claim's
`log_path`/`cwd`/`prompt` are gone the instant that release happens. Dead-letters on first offence
rather than retrying, since a missing/broken reconcile command will not fix itself between
sweeps. Tests: `tests/test_dispatcher.py`.

**Why this one:** its own docstring calls it "THE runaway net" — without it, a reconcile command
that resolves to nothing in the session's cwd (`claude -p` returning in ~24ms with `Unknown
command: ...` before any tool could run) respawns forever, since no event could possibly have
been appended to make the ticket look progressed or stuck. This is the single control whose
absence *alone* reproduces the 2026-07-19 shape most directly: a session that structurally cannot
make progress, respawned at whatever rate the other gates leave open.

### Backup/restore

`maestro/backup.py`, wired into `dispatch()` via `backup.maybe_backup`
(`maestro/backup.py:108`), cursor-gated exactly like the tracker/network hooks. Verbs: `maestro
backup`, `maestro backup --list`, `maestro restore` (`backup.restore_backup`,
`maestro/backup.py:166`). Tests: `tests/test_backup.py`.

**Why this one:** not a control against the 2026-07-19 runaway at all — it closes the *other*
2026-07-18 incident, the board wipe, which had no other copy of the event logs to fall back on.
It's included in this document's control set because both incidents landed within 24 hours of
each other and both drove the same "what controls exist now, and why" question this document
answers. Snapshots default to a *sibling* of the home, specifically so a repeat of the 2026-07-18
`rm -rf` on the home itself leaves them intact.

## 4. Gaps: the two controls added only after the first round shipped

The first hardening round (GA-5/GA-8/GA-11/RB-12/ratelimit/turn-budgets/`detect_zero_turn_spawns`/
backup) closed root causes #1-#4 directly. Two further gaps surfaced only after that round was
already in place, and each stayed invisible for a structurally different reason.

### GA-14 — the spawn ledger only counted top-level sessions

Before RF-7, the Implementer↔QA loop (AD-4/T-23) ran entirely *inside one* `implementing` spawn,
via `Agent`-tool sub-agent calls — up to `max_impl_turns` rounds, each firing a QA sub-agent the
dispatcher's own spawn ledger never saw, because the ledger only records what `dispatch()` itself
spawns. A single `implementing` session could therefore consume many agent-equivalents of real
work and cost while counting as exactly one entry against every rate control in §3 — GA-5's
budget, GA-8's floor, the ratelimit gate's candidate set all read the ledger, and the ledger was
wrong for this one phase. This is precisely the scattering this document's own Intent names: the
control set could be read as a set and still miss this, because the miscount lived in what the
ledger *didn't* record, not in any control's own logic.

What made it invisible: every individual control was doing exactly what it claimed to do,
correctly, against the wrong denominator. RF-7 fixed the denominator at the source — moving the
QA loop into its own dispatcher-spawned `qa` phase, so each round became a real, ledger-visible
spawn (`dispatcher.spawn_weight`'s docstring, `maestro/dispatcher.py:2029-2039`) — which is why
`implementing` itself weighs a flat 1 today (§3). GA-14 is the residual: `qa` can still fan out
one legal `Agent`-tool sub-agent for the Standards axis (`cfg.qa_standards_axis`), so `qa` weighs
1 or 2, never more, and `health._budget_weight` deliberately assumes a **smaller** per-spawn
weight than `dispatcher.spawn_weight`'s own worst case — multiplying both sides of `rate >
budget` by the same constant never changes which side crosses first, so budgeting for the
worst-case weight as the *healthy baseline* would make the weighted detector no more sensitive
than the session-counting one it replaces.

### The identical-tree bounce loop — found, not yet shipped

A companion investigation (a weak-model harness built to score reconciler robustness against
smaller/local models, `lab/`) found a fourth class of loop that evades every control in §3
simultaneously: a session that re-advances a phase transition against a tree state that has
already been judged, with no actual change since. Each bounce advances `observed_seq` (so the
no-progress watchdog resets), is not itself a `Failed` event (so `max_failures` never accrues),
and — if the bouncing session respawns no faster than the floor — lands exactly at a rate every
rate-based gate in §3 treats as legitimate throughput. A fix (`max_identical_tree_bounces`,
counting consecutive red routings at an identical tree state and dead-lettering the ticket at the
threshold) exists and has been validated against the lab's own harness, but **it has not been
merged into `main`** — this ticket's own drift-guard test (§5) is exactly why this document does
not list it in §3 as a wired control: doing so would cite a symbol that does not resolve in the
package, which is precisely the failure mode §5 exists to catch. It is recorded here, instead of
silently dropped, because it is a real, measured gap in the shipped control set today — the
inverse mistake to inventing a control would be to omit a genuine open one.

What makes it invisible to the *shipped* set: it evades the failure signal every other control
keys off (`Failed` events, frozen `observed_seq`, exceeded rate) by construction — a bounce
"succeeds" at each of those measures individually while making no real progress. It is closer in
shape to RB-11's burn detection (`maestro/burn.py`, a *different*, already-shipped 2026-08-14
incident's control, keyed on repeated byte-identical `Failed` text or a frozen `observed_seq`)
than to any control in §3 — but RB-11 doesn't catch it either, for the same reason: a bounce
changes `observed_seq` on every cycle and need not repeat identical failure text.

## 5. Posture

Like `docs/formal-methods-evaluation.md`, this is a dated, one-off record, not something kept
in lockstep with the package by construction — the prose here will not update itself the day a
cited module is renamed. What *is* guarded, cheaply, is the one thing that would otherwise rot
silently and be worse than no document at all: the control-set citations above resolving to
nothing. `tests/test_postmortem_drift.py` extracts every backticked module-dot-symbol and
cfg-dot-field citation from this file (e.g. `dispatcher.spawn_weight`, `cfg.max_impl_turns`) and
asserts each still exists as an importable attribute or a `Config` dataclass field, respectively —
mirroring `tests/test_diagram.py`'s posture of failing
`make test` the moment a generated/cited artifact drifts from its source, without trying to
regenerate this document's prose the way that suite regenerates `docs/state-machine.md`.
