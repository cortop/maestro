# The board as a resource-constrained workflow net

**Date:** 2026-08-13 · **Status:** one-off analysis, no code changed · **Verdict:** the board is
usefully a resource-constrained workflow net (RCWF-net) as a *lens*, not as a *maintained
artifact* — promoted from `tickets/MTO-9/proposal.md`, approved 2026-08-13.

This is a companion to [`docs/formal-methods-evaluation.md`](formal-methods-evaluation.md), same
posture: it records why a checked Petri net was **not** built, not a spec to keep in sync with the
code. It is dated and answers a question that was live on 2026-08-13; treat every code-line
reference below as of that date, and re-derive rather than trust them if the surrounding code has
since moved. **Not maintained** — nothing here is drift-guarded, nothing regenerates it, and no
test fails if it goes stale. If the board's resource layer changes materially, write a new dated
analysis rather than editing this one.

---

## 1. The board as a place/transition system

`maestro/statemachine.py` is a per-*case* net: 11 places, an explicit `TRANSITIONS` table
(`maestro/statemachine.py:41-58`), a source (`triaging`) and a sink (`done`) with `TERMINAL_PHASES
= {DONE}` (`:32`) — textbook van der Aalst WF-net shape. The board adds static places shared by
every case. Enumerated from the code:

| Shared resource | Where | Net encoding | PN class needed |
|---|---|---|---|
| `max_concurrency` | `dispatcher.py:1878` (`slots = max_concurrency - len(active)`) | semaphore place, capacity 12 | P/T |
| per-key claim | `claims.py:54`, written `sessions.py:235` | one mutex place per colour | **coloured** |
| `min_spawn_interval` (spawn ledger `last`) | `dispatcher.py:1830-1840`, `_spawn_ledger_path` `:1408` | per-colour **age** guard on a token | **timed-arc** |
| `max_spawns_per_sweep` (per repo) | `dispatcher.py:1858-1876` | place refilled to `cap` every sweep | timed / reset |
| `daily_spend_ceiling_usd` | `spend.over_ceiling`, gate `dispatcher.py:1806` | **real-valued** marking, reset at UTC midnight | continuous / hybrid |
| runaway brake + `fleet.pause_state` | `dispatcher.py:1800`, `fleet.py:260`, gate `:1658` | global **inhibitor** arc with a deadline | inhibitor + time |
| rate-limit pause | `ratelimit.paused_until`, gate `dispatcher.py:1794` | same | inhibitor + time |
| `dependsOn` | `_has_unmet_deps` `dispatcher.py:1364`, enforced only for `READY` at `:237` | inhibitor arc reading *another colour's* place | **inhibitor** |
| `max_spawn_attempts` | `_allow_spawn` `dispatcher.py:1340` | per-colour counter reset on `observed_seq` advance | coloured counter |

The sweep is one transition-firing round: 21 gates in a fixed order from "found on board" to
"subprocess spawned" (`dispatcher.py:1650` → `:2047`). The 15 rejection outcomes are already
string literals in the code (`dispatcher.py:1838, 1853, 1872, 1882, 1904, 1907, 1928, 1941, 1955,
1989, 2003, 2007, 2018, 2030, 2049`) and are already persisted per key to `derived/dispatch.jsonl`
(`dispatch_ledger_path` `:1566`), readable with `maestro why` (`cli.py:754`). The *runtime* half of
the resource layer is documented; the *static* half — this section — was not, before now.

### The verdict

**The formalism this needs is Turing-complete.** Inhibitor arcs alone make P/T nets equivalent to
counter machines; add colours, time and a real-valued place and nothing is decidable. Even the
untimed, uncoloured skeleton lands in a class with known negative results:

- **Soundness of a WF-net** = liveness + boundedness of the short-circuited net (van der Aalst,
  *Verification of Workflow Nets*, 1997). For maestro, "sound" is exactly the property the board
  wants: every ticket retains the option to reach `done`, and nothing spins forever.
- **RCWF-nets** — a WF-net plus static resource places — are the exact match. The soundness
  problem is **undecidable** when there is more than one static resource place and instances may
  terminate having created or consumed resources; it becomes decidable when **the number of cases
  is bounded**, or when there is exactly one resource type (van Hee, Serebrenik et al.; the
  *interval soundness* line of work recovers decidability by a home-space reduction).
- Where analysis *is* decidable, it rests on Petri net reachability, settled in 2021 as
  **Ackermann-complete** — not primitive recursive (Czerwiński & Orlikowski, FOCS 2021).

maestro has **eight** static resource places (the table above) and an unbounded arrival stream of
tickets. So the honest formal statement is: *the general question about this board is undecidable,
and the tractable restriction is "simulate K tickets".* That is
`docs/formal-methods-evaluation.md` §2 — "maestro's proof-shaped surface is finite, so a `for` loop
beats a kernel" — recovered from a different direction. §2 argued it from the *size* of the
per-ticket domain; the RCWF result argues it from the *theory* of the multi-ticket domain: the
bounded-case fragment is not merely the cheap option, it is the only decidable one.

---

## 2. The incident test — would a Petri net model have gone red?

This is the bar `docs/formal-methods-evaluation.md` set for itself. Assume the artifact under
evaluation: a coloured, timed net of the board, hand-transcribed from `dispatcher.py`, checked for
soundness/boundedness on a bounded number of cases.

| # | Incident | Nature | Model verdict |
|---|---|---|---|
| 1 | **2026-07-19 runaway** — requeue timer tested inside the `SLEEPING_PHASES` arm | per-ticket predicate bug; multi-ticket blast radius | **Green.** A net transcribed from `dispatcher.py:104` (pre-`8a25965`) reproduces the branch order and agrees with it. There is an affirmative reading — pre-fix, nothing consumed from a spawn-budget place, so a coverability check on a net containing such a place reports it unbounded — but that net only exists if you had already modelled the resource whose absence *was* the bug. Coverability would have answered a question nobody had asked. The property that has power — "a future requeue timer holds every non-terminal phase" — is model-independent and now sits at `tests/test_dispatcher.py:96` and `tests/test_dispatcher_exhaustive.py:114`. |
| 2 | **MTO-1** — `git worktree add` killed by a 30s timeout at ~56s; `wt.exists()` used as the completion witness | crash-atomicity across a process boundary | **Green.** Expressible (split `create` into `files_written` / `index_written`, add a timeout transition between), but only if you already suspect the interruption. The technique that finds this is IO fault injection (`tests/fault_injection.py`), not a net. |
| 3 | **MTO-2** — base-drift livelock, `awaiting-ci` → `implementing` → `awaiting-ci` forever | genuine livelock over a shared resource — the strongest case for the affirmative | **Partial, and it does not survive contact.** The net names the property precisely: `done` is not a home marking, i.e. the WF-net is not sound. But the cycle is *legitimately reachable* — rebasing a genuinely stale branch is correct behaviour. What made it a livelock is a rate mismatch between two clocks outside the system (`origin/preprod` at one commit per ~95s vs. a 20–40 min pipeline). An untimed net flags a cycle that also exists in the fixed system → false positive; a timed net needs a third-party repo's commit velocity as a parameter. The actual fix (`base_drift_policy` defaulting to `on_conflict`, `dispatcher.py:936-960`, plus the pending-CI block at `:943`) was found by watching a real board. |
| 4 | **T-44** — adopted `maestro/T-4` from a previous board incarnation | identity collision on a namespace shared with the outside world | **Green.** A coloured net's colours are distinct by construction; the defect is that two different cases *aliased onto one colour* because git refs outlive the board — out of reach of the net for the same reason path/case aliasing is out of reach in `docs/formal-methods-evaluation.md` §4(g),(h). |
| 5 | **T-45** — 0-turn spawn, `is_error: false`, 215/341 spawns burned in an hour | detection gap + an ordering constraint | **Green for the defect.** The failure signal lived in the session log, not the event log the FSM reads; a net of the pre-fix system has no `detect_zero_turn` transition to starve. The fix's *ordering* invariant — `detect_zero_turn_spawns` must fire before `list_active()` consumes the claim token (`dispatcher.py:1722-1723`) — is exactly a token-competition property a net expresses well. It is currently a code comment plus `tests/test_dispatcher.py:1855`. |

**Score: 0 clean catches out of 5.** Two partials, both of which reduce to "the model gives you
vocabulary", not "the model gives you a verdict". Recorded honestly, because it is the reason a
checked net is not being built: the hand-transcribed model inherits the code's own bugs (it is
read branch-for-branch from the same source), and the incident that looks like the strongest case
for the affirmative (MTO-2) turns out to hinge on a rate mismatch between two external clocks that
no untimed net could see and no timed net could parameterize without already knowing the answer.

---

## 3. What the lens actually bought: candidate resource-layer defects

Reading the sweep as a place/transition system for one afternoon surfaced four resource-layer
issues. **These are reasoned from the code, not reproduced by a test** — each needs its own ticket
to confirm or dismiss; they are not asserted here as verified defects.

1. **`max_concurrency` has no mutual exclusion.** `store.file_lock` appears exactly four times in
   the package (`event_log.py:91`, `inbox.py:24,34`, `ops.py:1197`, `dispatcher.py:1578` — the
   last is the decision *ledger*, not the sweep). Nothing serialises `dispatch()`. Two sweeps can
   overlap: launchd's tick and the in-process `_nudge` that fires on every `maestro ans` / `cmd` /
   `create` (`cli.py:118`, invoked `:270, 299, 311, 321, 378`). Both read `active` at `:1736`, both
   compute `slots = max_concurrency - len(active)` at `:1878`, both spawn. In net terms the
   semaphore's test-and-decrement is not one transition.
2. **The claim is written *after* `Popen`, not before.** `sessions.py:224` launches;
   `sessions.py:235` writes the claim. Two overlapping sweeps both see the key absent from
   `active` (gate at `:1774`), both launch, and the second `write_claim` overwrites the first —
   leaving one live reconciler with **no claim**: invisible to `max_concurrency`, invisible to
   `run_watchdog` (which iterates `all_claims`). Note `_nudge`'s own docstring asserts the
   opposite: *"The existing per-key claim dedup prevents double-spawning if a reconciler is
   already live"* (`cli.py:90-91`). The event log's per-key fencing is what keeps this from
   corrupting state (`claims.py:19-23`) — but the *cost* and *concurrency* consequences are
   unfenced.
3. **`.spawn_ledger.json` is a last-writer-wins whole-file replace** (`dispatcher.py:2062` via
   `store.atomic_write`). An overlapping sweep can drop another key's `last`, defeating
   `min_spawn_interval` for it — the very floor built in response to the 2026-07-19 runaway.
4. **`max_spawns_per_sweep` counts intent, not spawns**, and `max_concurrency` truncates by
   natural key order. `per_repo_spawn_count[name] = seen + 1` at `:1874` runs before gates
   G14–G21 can reject the key, so a repo whose keys all fail the credential gate still burns its
   cap. And `to_spawn = eligible[:slots]` (`:1879`) slices a list sorted by `split_key` (`:284`) —
   low-numbered keys structurally starve high-numbered ones under sustained pressure, with
   `priority:` in the spec frontmatter having no effect.

(1)–(3) are boundedness and mutual-exclusion violations over shared resources; (4) is a fairness
violation. This is the whole affirmative case turned up by the lens, and it is a case for the
*lens*, not the tool — no net was built to find any of them.

---

## Sources

**Primary — this repo**
- `maestro/statemachine.py:11-58` — the 11 phases, `SLEEPING_PHASES`, `TERMINAL_PHASES`, `TRANSITIONS`: the per-case WF-net.
- `maestro/dispatcher.py:218-263` — `is_due`; `:239-244` is the in-code account of the 2026-07-19 runaway.
- `maestro/dispatcher.py:1650-2083` — the ordered gate list; `:1794` ratelimit, `:1800` runaway brake, `:1806` spend ceiling, `:1830-1840` spawn floor, `:1858-1876` per-repo cap, `:1878` `max_concurrency`, `:2049` spawn.
- `maestro/dispatcher.py:1566, 1575-1584` + `maestro/cli.py:754` — `derived/dispatch.jsonl` and `maestro why`: the resource layer's existing *runtime* documentation.
- `maestro/sessions.py:224-236` — `Popen` precedes `write_claim`; `maestro/cli.py:87-118` — the in-process `_nudge` sweep and its mutual-exclusion claim.
- `maestro/claims.py:19-23, 54-93, 238-273` — claims are a hint, reclaimed by a live `ps` scan.
- `maestro/store.py:150-171` — the only lock primitive, and `atomic_write`'s whole-file replace.
- `docs/formal-methods-evaluation.md` §2, §3, §4, §6, §7 — the prior art this analysis engages; §7's table supplies the rank-2 technique (deterministic simulation) recommended as the follow-on to this ticket.
- `tests/test_dispatcher_exhaustive.py:63-71, 113-167` — the 1056-point single-ticket grid and its "has teeth" mutant; `tests/test_dispatcher.py:96, 136` — the runaway's two regression tests.
- `DESIGN.md:81-89`, `README.md:37-42` — the two hand-drawn ASCII phase graphs and the drifted `SLEEPING_PHASES` prose.
- `tickets/MTO-1/spec.md`, `tickets/MTO-2/spec.md`, `tickets/T-44/spec.md`, `tickets/T-45/spec.md` (this board) — incident records; fixes at `maestro/ops.py:306, 364, 545`, `maestro/dispatcher.py:936-960`, `maestro/ops.py:254`, `maestro/dispatcher.py:1286`.
- `tickets/MTO-9/proposal.md` — the approved proposal this document was promoted from, in full (§4(B)/(C) recommend the multi-sweep simulator and the generated diagram as separate, later tickets).

**Primary — external**
- <https://doi.org/10.1109/5.24143> — T. Murata, *Petri Nets: Properties, Analysis and Applications*, Proc. IEEE 77(4):541–580, 1989. The definitional source for boundedness, liveness, reachability and the coverability-tree method invoked in §2.
- <https://www.vdaalst.com/publications/p44.pdf> — W.M.P. van der Aalst, *Verification of Workflow Nets*. Defines WF-nets (source `i`, sink `o`) and the theorem that soundness ≡ liveness + boundedness of the short-circuited net — the property this document's follow-on (a bounded simulator) would assert as a bounded search.
- <https://link.springer.com/chapter/10.1007/978-3-642-21461-5_17> — van Hee, Serebrenik et al., *Dynamic Soundness in Resource-Constrained Workflow Nets*, and <https://link.springer.com/chapter/10.1007/978-3-642-40213-5_10> — *Interval Soundness of Resource-Constrained Workflow Nets: Decidability and Repair*. The undecidability of soundness with several static resource places, and the decidable restrictions (bounded case count; one resource type; interval soundness via home-space reduction). This is the load-bearing citation for §1's conclusion.
- <https://arxiv.org/abs/2104.13866> — Czerwiński & Orlikowski, *Reachability in Vector Addition Systems is Ackermann-complete*, FOCS 2021. Petri net reachability is not primitive recursive; every decidable soundness algorithm that reduces to it inherits this.
- <https://arxiv.org/abs/2206.02606> — Blondin et al., *Verifying generalised and structural soundness of workflow nets via relaxations*. States the k-soundness PSPACE/EXPSPACE-completeness landscape and that existing generalised/structural-soundness algorithms rest on Ackermann-complete reachability.
- <https://github.com/tapaal/verifypn> and <https://www.tapaal.net/> — TAPAAL / `verifypn`, the timed-arc Petri net toolchain a checked-net alternative would need; a C++ engine consuming PNML, gold medallist at the 2024 Model Checking Contest.
- <https://www.pnml.org/> — PNML, ISO/IEC 15909-2, the interchange format a checked-net alternative would have to commit.
- <https://snakes.ibisc.univ-evry.fr/> and <https://pypi.org/project/SNAKES/> — SNAKES (Pommereau), a Python Petri net library: LGPL, 0.9.33 (2024-06-03), a simulator rather than a checker.
