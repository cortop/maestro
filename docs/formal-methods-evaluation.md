# Formal methods for maestro: evaluating Lean 4

**Date:** 2026-08-08 · **Status:** evaluation, no code change · **Verdict:** do not build a Lean layer

The question: could [Lean 4](https://lean-lang.org/) — an open-source programming language and
proof assistant with a minimal trusted kernel — make maestro's correctness-critical core provably
robust?

Short answer: **no, not at this scale.** There is exactly one narrow slice where Lean would earn
its keep (§6), and it is third in line behind work that is roughly 20× cheaper and reaches the
defects Lean structurally cannot see. This document records the evidence, because the reasoning
generalises to the next formal-methods proposal too.

Everything below marked *reproduced* was run against the real package on a temporary
`MAESTRO_HOME`, never against a real board.

---

## 1. What Lean is actually good at

Lean's comparative advantage is narrow and specific: **an infinite domain with a property
universally quantified over an inductive structure.** Structural induction over arbitrary-length
lists is something no amount of testing reaches.

Two corollaries decide most of this evaluation:

- Where the domain is **finite**, `decide` is the same brute force as a `for` loop, with a
  ~525 MB toolchain attached.
- Where the domain is **concurrent or crash-interleaved**, a model checker (TLA+, Alloy) models
  the lock, the crash and the adversary; Lean would need a hand-built filesystem model first, at
  which point TLA+ is cheaper.

Lean is also a genuinely compiled language (it emits C, has a documented FFI, and AWS Cedar ships
a Lean model differential-tested against the Rust implementation). That path is real. It is also
irrelevant here — see §5.

---

## 2. Finding: maestro's proof-shaped surface is finite, so a `for` loop beats a kernel

`statemachine.py` is a 10-constructor enum with an explicit transition table. `dispatcher.is_due`
is a total function of `(phase, snapshot fields, now)`. Both are finite. Every theorem a
`Phase.lean` / `Due.lean` model would carry is decidable by enumeration against the **real**
functions, in milliseconds of stdlib Python:

```python
assert set(TRANSITIONS) == set(Phase)                                  # table complete
assert closure(Phase.TRIAGING) | {Phase.TRIAGING} == set(Phase)        # all reachable
assert all(Phase.DONE in closure(p) for p in Phase if p != Phase.DONE) # DONE co-reachable
assert not (SLEEPING_PHASES & TERMINAL_PHASES)                         # classification disjoint
assert SLEEPING_PHASES | TERMINAL_PHASES | ACTIVE_PHASES == set(Phase) # classification covers
```

All five hold today. So does the exhaustive `is_due` sweep — 960 input points
(`phase × inbox_pending × open_questions × answered_questions × pending timer`), 0 violations of
"a future requeue timer holds every non-terminal phase", ~2 ms. The only `due=True` row is
`awaiting-human/stranded`, which is the deliberate safety net at `dispatcher.py:140`.

`decide` over the same space buys a smaller trusted computing base for a computation whose risk
was never CPython.

## 3. Finding: a Lean model would have been green during the $845 runaway

Any Lean model of `is_due` gets written by reading `dispatcher.py` branch-for-branch. The
2026-07-19 runaway was a **correct check in the wrong `if` branch** — the requeue-timer test sat
inside the `SLEEPING_PHASES` arm, so a `maestro requeue` from an active phase was ignored and the
ticket came back due on the very next sweep (21,731 no-op sessions, 5,522 discarded
`RequeueScheduled` events, ~$845).

A model transcribed from that code would have agreed with that code, and its conformance vectors
would have passed, for all 35 hours the incident ran.

What has power there is the **property statement** — "a future requeue timer holds every
non-terminal phase" — and that statement is Lean-independent. It is already in the suite at
`tests/test_dispatcher.py:96`; the entire marginal yield of the Lean flagship theorem is widening
that `parametrize` list to `list(Phase)`.

The deeper category error: the runaway's harm was **cost, not incorrectness**. The fleet did
exactly what the code said. No proof about a decision function bounds spend — only a meter and a
ceiling do. That is what GA-5, GA-8 and GA-11 are, and they bound blast radius regardless of which
logic bug caused the loop.

## 4. Finding: every reproduced defect lives where Lean cannot reach

All eight reproduced against the real package (see §8 for the harness):

| # | Defect | Evidence | Site |
|---|---|---|---|
| a | **Torn tail swallows the next append and disarms step-id dedup** | append reports `seq 2`; `read()` returns `[1]`; the *same* `step_id` is then accepted a second time | `store.append_line` (`store.py:189`) |
| b | **`fold` is not total** | `PhaseChanged{phase:"bogus"}` → `ValueError`; missing `phase` → `KeyError`. The per-key sweep loop has no `try/except`, so one corrupt event stops dispatch **for every other ticket** | `snapshot.py:170` |
| c | **`fold` is not duplicate-idempotent** | one `Failed` → `failure_count=1`; the same list doubled → `2`. A crashed `compact` leaves events in both files, inflating the counter into `max_failures` and dead-lettering a healthy ticket | `snapshot.py:222` |
| d | **`observed_seq` is last-write, not high-water** | `fold([seq=9, seq=1]).observed_seq == 1` | `snapshot.py:152` |
| e | **`DONE` is not absorbing** | `[Finalized, Stalled]` → `"degraded"`, an *active* phase | `snapshot.py:225` |
| f | **`step_id` encoding is not injective** | `step_id("K\x1fp","q",1,"a") == step_id("K","p\x1fq",1,"a")` — `\x1f` delimiter injection | `idempotency.py:19` |
| g | **Path aliasing** | `events_path(h,"X.archive") == events_archive_path(h,"X")` — one file, one seq counter, one dedup set, two different lock files | `store.py:51,55` |
| h | **Case aliasing on APFS** | keys `CASE-1` and `case-1` share one stream; `read("case-1")` returns both tickets' events | `store.py:21` |

Two further findings from reading call sites rather than proving anything:

- **The fencing CAS is dormant in production.** No call site passes `expected_last_seq`:
  `cmd_set_phase` (`cli.py:582`) never forwards an `--expect`, no `ops.set_phase` caller passes
  one, and no reconcile skill uses `maestro append --expect`. `DESIGN.md`, `event_log.py`'s
  docstring and `claims.py:20` all describe a fencing-gated log; what runs is a blind append
  protected by the per-key lock plus step-id dedup. **Formalising the documented protocol would
  prove properties of code that does not execute.**
- **`atomic_write`'s temp name is pid-only** (`store.py:170`), while the TUI runs `ops.compact` /
  `dispatch` / `project` on worker threads of a single process (`tui/app.py:344,368`) — same pid,
  same temp path, concurrent replace.

And one durability gap in the same family: `ops.compact` (`ops.py:658-667`) appends to the archive
**without fsync**, then `tmp.replace()`s the active log. A crash in that window loses events from
the sole source of truth.

None of these is reachable by a proof about a pure function. They are durability, encoding,
filesystem-semantics and totality defects — precisely the impure shell that every Lean design
correctly declares out of scope. A technique whose scope boundary falls exactly where the bugs
aren't is choosing its problem rather than letting the problem choose the technique.

## 5. Why not compile Lean into the runtime

Considered and rejected on its own merits, independent of the above:

- **Codegen (Lean → generated Python).** The emitter is unverified and its failures are
  *correlated* — one emitter bug corrupts every generated function simultaneously, where a human
  transcription slip corrupts one. So differential vectors are still required to check the
  emitter, at which point the vectors deliver the whole benefit and the codegen is pure added
  surface. Generated code in the correctness-critical core also inverts the readability property
  that makes "deterministic plumbing in Python" trustworthy.
- **FFI (Lean → C shared library → `ctypes`).** Breaks the stdlib-only, no-runtime-deps core;
  requires per-platform build artifacts; and trades a Python traceback for a SIGSEGV under
  launchd, which is a strict downgrade of the fail-loud property maestro sells.

## 6. The only Lean slice worth funding, if ever

After the remediation in §7 lands, and only then:

Extract `step : Snapshot × Event → Snapshot` in Python (`snapshot.fold` is currently an inlined
`for` loop, so nothing can bind to it), model *that* in Lean, and prove the four laws
**universally quantified over unbounded event lists**:

1. incrementality — `fold(a ++ b) = foldl step (fold a) b`
2. duplicate-idempotence — `fold(dedup_by_seq(evs)) = fold(evs)`
3. `observed_seq` monotonicity
4. phase closure — `∀ evs. fold(evs).phase ∈ Phase`

Laws 2–4 are currently **false** (defects c, d, b). This is the one place sampling genuinely
cannot substitute for induction: the proof is what tells you the fix is complete across all ~30
snapshot fields, not just the two counters someone remembered. It also pays for itself —
incrementality is the licence to fix the O(n²) in `ops._append`, which calls `snapshot.rebuild`
(a full re-fold of archive *plus* active log) on **every single append**.

Constraints if it is ever built: core Lean only, no Mathlib, no `require` stanzas (headless
reconcilers have no network), `native_decide` banned with a CI grep, vectors committed as build
artifacts so `make test` stays stdlib + pytest, CI path-filtered.

**Kill criteria.** Abandon it if either fires:

- the phase-vocabulary conformance assertion is ever `xfail`ed — the proofs become decorative the
  moment the model stops being bound to the Python;
- a reconciler parks in `awaiting-human` twice because it cannot discharge a proof. `dispatcher.py`
  appears in roughly a third of recent commits, and an LLM reconciler writes competent Python and
  unverifiable Lean. That failure mode — an agent looping without progress — is the same shape as
  2026-07-19.

## 7. What to do instead

In order. Items 2 and 3 are the defect fixes from §4.

1. **Set `daily_spend_ceiling_usd`** on the dogfood board. See §9 — it is currently unset.
2. **Fix `append_line`'s torn tail** — seek to end, read the last byte, prepend `\n` if it is not
   a newline. One line. This defect defeats the idempotency argument `idempotency.py`'s docstring
   rests on.
3. **Close the remaining §4 defects** — make `fold` total; dedup by seq in `event_log.read`;
   `observed_seq` → `max`; guard `Stalled`/`PhaseChanged` on `phase != done`; escape the `step_id`
   separator; reject a trailing `.archive` and normalise case in `validate_key`; unique temp name
   in `atomic_write`; fsync the archive before the active-log replace in `ops.compact`; and
   resolve the dormant fencing CAS — wire it up or delete the parameter and correct the docs.
4. **Exhaustive stdlib enumeration of `is_due` + the phase-graph assertions** from §2. Half a day,
   no toolchain. This is the real payload of the Lean proposal.
5. **Hypothesis over `fold`** for the algebraic laws — the ones now known to be false.
6. **A crash-injection harness** over the append/compact path: kill between the archive append and
   the active-log rewrite, kill mid-line, `flock` unavailable, `fsync` no-op. This is the
   technique that found defect (a) in ten minutes.
7. **A spawn-rate / spend alarm that reaches a human within an hour.** The runaway went 35 hours
   undetected while the log screamed 5,522 discarded `RequeueScheduled` events. That detection gap
   is still open.

Ranking of the techniques considered, by robustness per unit effort for *this* codebase:

| # | Technique | Effort | Catches maestro's real bug classes? |
|---|---|---|---|
| 1 | Property/exhaustive tests on `is_due` and `fold` | hours | yes |
| 2 | Deterministic simulation (FoundationDB / TigerBeetle style) | 2–5 days | yes — emergent multi-sweep gate interactions |
| 3 | IO-layer fault injection | 1–2 days | yes — found defect (a) |
| 4 | Hypothesis `RuleBasedStateMachine` over the phase machine | 1–2 days | partly |
| 5 | Runtime invariant assertions | hours | partly — turns silent corruption loud |
| 6 | `mypy --strict` on the core modules only | 1–3 days | weakly |
| 7 | TLA+/PlusCal, Quint | 1–3 weeks | no — would have verified the buggy design as correct |
| 8 | Alloy | ~1 week | no |
| 9 | Model-based testing (model → traces → replay) | 3–6 weeks | only after 7 exists |
| 10 | **Lean 4** | weeks–months | no, except the §6 slice |

## 8. Reproduction

The defect table was produced by importing the real package against a temp home. The load-bearing
snippets:

```python
# (a) torn tail — swallowed append + disarmed dedup
event_log.append(home, "T1", "Note", {"t": "1"}, actor="x", step_id="sid-1")
with store.events_path(home, "T1").open("a") as fh:
    fh.write('{"seq": 2, "type": "Note", "payload": {"t": "torn"')   # no newline: a crashed writer
ev = event_log.append(home, "T1", "Note", {"t": "3"}, actor="x", step_id="sid-3")
assert ev["seq"] == 2                                                 # reports success
assert [e["seq"] for e in event_log.read(home, "T1")] == [1]          # the event is gone
assert event_log.append(home, "T1", "Note", {}, actor="x", step_id="sid-3") is not None  # dedup disarmed

# (b) fold is not total
snapshot.fold("K", [{"seq": 1, "type": "PhaseChanged", "payload": {"phase": "bogus"}}])  # ValueError
snapshot.fold("K", [{"seq": 1, "type": "PhaseChanged", "payload": {}}])                  # KeyError

# (c) fold is not duplicate-idempotent
evs = [{"seq": 1, "type": "Failed", "payload": {"error": "x"}}]
assert snapshot.fold("K", evs).failure_count == 1
assert snapshot.fold("K", evs * 2).failure_count == 2

# (d) observed_seq is last-write, not high-water
assert snapshot.fold("K", [{"seq": 9, "type": "Note"}, {"seq": 1, "type": "Note"}]).observed_seq == 1

# (e) DONE is not absorbing
assert snapshot.fold("K", [{"seq": 1, "type": "Finalized", "payload": {}},
                           {"seq": 2, "type": "Stalled", "payload": {"reason": "r"}}]).phase == "degraded"

# (f) step_id encoding is not injective
assert step_id("K\x1fp", "q", 1, "a") == step_id("K", "p\x1fq", 1, "a")

# (g) path aliasing
assert store.events_path(h, "X.archive") == store.events_archive_path(h, "X")

# (h) case aliasing (case-insensitive filesystem)
event_log.append(home, "CASE-1", "Note", {}, actor="x")
event_log.append(home, "case-1", "Note", {}, actor="x")
assert len(event_log.read(home, "case-1")) == 2
```

## 9. Operational finding

Measured on the dogfood board on 2026-08-08:

```
daily_spend_ceiling_usd = None        # GA-11's gate fails open — unset in config.toml
spend today             = $58.36
spawn_budget            = 780/hour    # GA-5 IS armed (derived default, not disabled)
```

The daily USD ceiling built in response to the $845 incident is not set on the board it exists to
protect. One line in `config.toml` buys a hard cap. Note that `runaway_spawns_per_hour = None`
does **not** disable the auto-brake — `health.spawn_budget` derives a budget from the effective
spawn floor and board composition — so GA-5 is live; only the dollar ceiling is absent.

## 10. What none of this catches

Worth stating plainly, because it bounds every programme above: agent behaviour and its cost.
Every technique here verifies the *plumbing*. maestro's governing principle is "deterministic
plumbing in Python, intelligence in Claude", and the intelligence half is not verifiable by these
means — it is bounded by rate limits, spend ceilings, approval tiers and blast-radius controls,
which is why those outrank proofs on this system.
