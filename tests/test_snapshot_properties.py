"""RB-10: property-based tests for `snapshot.fold`'s algebraic laws.

`fold` is a pure function over an *unbounded* list of events -- the one part of maestro whose
properties genuinely cannot be established by enumeration (contrast RB-9's exhaustive `is_due`
sweep, where the input space is finite). Sampling is the right tool here: generate arbitrary
event logs -- valid events of every type in `events.py`, plus malformed variants (missing keys,
wrong types, unknown event/phase strings, duplicate/out-of-order seqs, empty logs) -- and check
the four laws RB-2 fixed hold for all of them, not just the hand-written lists in
`test_snapshot.py`.

Hypothesis is a `dev`-extra-only test dependency (see pyproject.toml's `[project.optional-
dependencies]`) -- it is never imported by the runtime core. This whole module collects to
nothing (skipped, not errored) via `pytest.importorskip` below when it isn't installed, so
`make test` without the `dev` extra still passes.

Law 5 (incrementality -- `fold(a + b)` agrees with folding `a` then continuing with `b`) is
deliberately NOT stated here: `fold` is an inlined `for` loop with no extractable `step`, and
extracting one is a prerequisite for a much larger piece of work (see
`docs/formal-methods-evaluation.md` §6) that needs its own decision, not a side effect of this
ticket. Laws 1-4 only.
"""
from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import example, given, settings, strategies as st

from maestro import events as E
from maestro import snapshot as snap_mod
from maestro.statemachine import Phase

# Keep the added suite time bounded (BUDGET, ticket's Notes): this module alone runs in a few
# seconds at this cap -- see the PR body for the actual measured before/after of the full suite
# on this machine (the ticket's own "~90s today" wasn't reproducible here; state what's real).
MAX_EXAMPLES = 100

# Every @given below also passes `derandomize=True` (ticket's Notes: "seed it deterministically
# so a failure is reproducible") -- Hypothesis then derives its seed from the test's own
# bytecode instead of OS randomness, so the exact same example sequence (and hence the exact
# same counterexample, byte-for-byte) is drawn on every run/machine/CI worker, with no
# `.hypothesis` example database (usually gitignored, so absent on a fresh clone) required.

ALL_TYPES = [
    E.TICKET_CREATED, E.SPEC_OBSERVED, E.PHASE_CHANGED, E.FINALIZED,
    E.QUESTION_ASKED, E.QUESTION_ANSWERED, E.COMMAND_RECEIVED,
    E.PR_OPENED, E.PR_UPDATED, E.CI_OBSERVED, E.REVIEW_FEEDBACK_RECEIVED,
    E.IMPL_TURN, E.IMPL_STEP, E.AC_VERIFIED, E.APPROVED, E.AC_QA_VERDICT,
    E.RESEARCH_PROPOSED, E.JIRA_SYNCED, E.LINEAR_SYNCED,
    E.REQUEUE_SCHEDULED, E.FAILED, E.STALLED, E.NOTE,
]
VALID_PHASES = [p.value for p in Phase]

_TEXT = lambda n=10: st.text(max_size=n)  # noqa: E731 -- short local alias, used only below

# --- valid payloads, one strategy per type in events.py ---------------------
# `fixed_dictionaries(..., optional=...)` includes each optional key roughly half the time, so
# this alone already covers "valid event with some keys missing" -- the malformed generator below
# adds the rest (wrong types, unknown keys, unknown types, non-dict payloads).
_VALID_PAYLOAD = {
    E.TICKET_CREATED: st.fixed_dictionaries({}, optional={
        "title": _TEXT(), "source": _TEXT(), "spec_hash": _TEXT(8),
        "kind": st.sampled_from(["implementation", "research"]),
        "external_source": _TEXT(8), "external_id": _TEXT(8), "repo": _TEXT(8),
    }),
    E.SPEC_OBSERVED: st.fixed_dictionaries({}, optional={"spec_hash": _TEXT(8)}),
    E.PHASE_CHANGED: st.fixed_dictionaries({}, optional={
        "phase": st.sampled_from(VALID_PHASES), "reason": _TEXT(), "forced_by": _TEXT(8),
    }),
    E.FINALIZED: st.just({}),
    E.QUESTION_ASKED: st.fixed_dictionaries({}, optional={"qid": _TEXT(8), "text": _TEXT()}),
    E.QUESTION_ANSWERED: st.fixed_dictionaries({}, optional={"qid": _TEXT(8), "answer": _TEXT()}),
    E.COMMAND_RECEIVED: st.fixed_dictionaries({}, optional={
        "command": _TEXT(8), "args": st.lists(_TEXT(5), max_size=3),
    }),
    E.PR_OPENED: st.fixed_dictionaries({}, optional={
        "number": st.integers(min_value=1, max_value=9999), "url": _TEXT(), "draft": st.booleans(),
    }),
    E.PR_UPDATED: st.fixed_dictionaries({}, optional={
        "number": st.integers(min_value=1, max_value=9999), "draft": st.booleans(), "merged": st.booleans(),
    }),
    E.CI_OBSERVED: st.fixed_dictionaries({}, optional={
        "state": st.sampled_from(["passing", "failing", "pending", "unknown"]),
        "failing_checks": st.lists(_TEXT(8), max_size=3), "detail": _TEXT(),
        "error": st.sampled_from(["auth", "not_found", "unknown"]),
    }),
    E.REVIEW_FEEDBACK_RECEIVED: st.fixed_dictionaries({}, optional={
        "comment_id": _TEXT(8), "state": st.sampled_from(["CHANGES_REQUESTED", "APPROVED", "COMMENTED"]),
        "body": _TEXT(), "author": _TEXT(8),
    }),
    E.IMPL_TURN: st.fixed_dictionaries({}, optional={
        "turn": st.integers(min_value=0, max_value=30), "role": st.sampled_from(["implementer", "qa"]),
    }),
    E.IMPL_STEP: st.fixed_dictionaries({}, optional={
        "turn": st.integers(min_value=0, max_value=30), "role": st.sampled_from(["implementer", "qa"]),
        "kind": _TEXT(8), "tool": _TEXT(8), "summary": _TEXT(),
    }),
    E.AC_VERIFIED: st.fixed_dictionaries({}, optional={
        "ac_hash": _TEXT(8), "ac_index": st.integers(min_value=0, max_value=10), "ac_text": _TEXT(),
        "evidence": st.fixed_dictionaries({}, optional={"what": _TEXT(8), "where": _TEXT(8), "result": _TEXT(8)}),
    }),
    E.APPROVED: st.just({}),
    E.AC_QA_VERDICT: st.fixed_dictionaries({}, optional={
        "ac_hash": _TEXT(8), "ac_index": st.integers(min_value=0, max_value=10), "ac_text": _TEXT(),
        "verdict": st.sampled_from(["pass", "fail"]), "evidence": _TEXT(),
        "axis": st.sampled_from(["spec", "standards"]),
    }),
    E.RESEARCH_PROPOSED: st.fixed_dictionaries({}, optional={
        "proposal_path": _TEXT(), "alternatives": st.lists(_TEXT(8), max_size=3),
    }),
    E.JIRA_SYNCED: st.fixed_dictionaries({}, optional={
        "jira_updated_ts": _TEXT(8), "status": _TEXT(8), "last_comment_id": _TEXT(8),
    }),
    E.LINEAR_SYNCED: st.fixed_dictionaries({}, optional={
        "linear_updated_ts": _TEXT(8), "status": _TEXT(8), "last_comment_id": _TEXT(8),
    }),
    E.REQUEUE_SCHEDULED: st.fixed_dictionaries({}, optional={
        "at": st.floats(min_value=0, max_value=2_000_000_000, allow_nan=False, allow_infinity=False),
    }),
    E.FAILED: st.fixed_dictionaries({}, optional={"error": _TEXT()}),
    E.STALLED: st.fixed_dictionaries({}, optional={"reason": _TEXT()}),
    E.NOTE: st.fixed_dictionaries({}, optional={"text": _TEXT()}),
}
assert set(_VALID_PAYLOAD) == set(ALL_TYPES)  # generator covers every type events.py declares

_JUNK_SCALAR = st.one_of(st.integers(), _TEXT(6), st.none(), st.booleans(),
                         st.lists(st.integers(), max_size=2))
_JUNK_DICT = st.dictionaries(_TEXT(6), _JUNK_SCALAR, max_size=4)
# Seq deliberately includes non-integer and negative values -- `fold` reads `seq` with a bare
# `.get`, an `isinstance(seq, int)` guard, and a `>` comparison against the running high-water
# mark, all of which must tolerate garbage here (law 1: totality).
_SEQ = st.one_of(st.integers(min_value=-5, max_value=15), st.none(), _TEXT(3),
                 st.floats(allow_nan=False, allow_infinity=False))


@st.composite
def _valid_event(draw):
    """A well-formed event of a random type -- may still omit optional payload keys."""
    t = draw(st.sampled_from(ALL_TYPES))
    payload = draw(_VALID_PAYLOAD[t])
    seq = draw(st.integers(min_value=-5, max_value=15))
    return {"seq": seq, "type": t, "payload": payload}


@st.composite
def _malformed_event(draw):
    """One deliberately-broken event: unknown type, unknown/wrong-typed phase, non-integer
    turn, a missing `type`/`payload` key, a non-dict payload, extra/misspelled payload keys,
    or pure junk unrelated to the schema -- everything law 1 (totality) must survive."""
    seq = draw(_SEQ)
    kind = draw(st.sampled_from([
        "unknown_type", "missing_type", "unknown_phase", "bad_turn",
        "missing_payload", "non_dict_payload", "extra_keys", "misspelled_keys", "pure_junk",
    ]))
    if kind == "unknown_type":
        t = draw(_TEXT(15).filter(lambda s: s not in ALL_TYPES))
        return {"seq": seq, "type": t, "payload": draw(_JUNK_DICT)}
    if kind == "missing_type":
        return {"seq": seq, "payload": draw(_JUNK_DICT)}
    if kind == "unknown_phase":
        bogus = draw(st.one_of(_TEXT(8), st.integers(), st.none(), st.booleans()))
        return {"seq": seq, "type": E.PHASE_CHANGED, "payload": {"phase": bogus}}
    if kind == "bad_turn":
        t = draw(st.sampled_from([E.IMPL_TURN, E.IMPL_STEP]))
        bogus = draw(st.one_of(_TEXT(6), st.none(), st.lists(st.integers(), max_size=2),
                               st.floats(allow_nan=True, allow_infinity=True)))
        return {"seq": seq, "type": t, "payload": {"turn": bogus}}
    if kind == "missing_payload":
        t = draw(st.sampled_from(ALL_TYPES))
        return {"seq": seq, "type": t}
    if kind == "non_dict_payload":
        t = draw(st.sampled_from(ALL_TYPES))
        bogus = draw(st.one_of(_TEXT(8), st.integers(), st.none(),
                               st.lists(st.integers(), max_size=3), st.booleans()))
        return {"seq": seq, "type": t, "payload": bogus}
    if kind == "extra_keys":
        t = draw(st.sampled_from(ALL_TYPES))
        payload = {**draw(_VALID_PAYLOAD[t]), **draw(_JUNK_DICT)}
        return {"seq": seq, "type": t, "payload": payload}
    if kind == "misspelled_keys":
        t = draw(st.sampled_from(ALL_TYPES))
        payload = {f"_{k}": v for k, v in draw(_VALID_PAYLOAD[t]).items()}
        return {"seq": seq, "type": t, "payload": payload}
    return draw(_JUNK_DICT)  # pure_junk: no seq/type/payload relation to the schema at all


def events_lists(max_size=15):
    """The general-purpose generator: a mix of valid and malformed events, in any order, with
    seqs that collide and go out of order by construction (both drawn from a small range) --
    covers every AC'd variant: valid events of every type, malformed payloads, unknown event
    types, unknown phase strings, missing keys, wrong value types, duplicate seqs, out-of-order
    seqs, and (via `max_size=0` on the Hypothesis side, exercised implicitly by `min_size=0`
    below) empty logs."""
    return st.lists(st.one_of(_valid_event(), _malformed_event()), min_size=0, max_size=max_size)


@st.composite
def _realistic_events(draw, max_size=12):
    """Well-typed events only (valid type + valid-shaped payload), but with seqs drawn from a
    small range so genuine duplicate/out-of-order seqs occur by construction -- exactly the
    interrupted-`ops.compact` shape the duplicate-idempotence law exists for (see
    `test_duplicated_seqs_from_interrupted_compaction_cannot_inflate_failure_count` in
    test_snapshot.py). Deliberately excludes the malformed generator's non-integer seqs: `fold`
    only dedups on an *integer* seq (`isinstance(seq, int)`), so an event with no seq at all is
    never deduped even if replayed twice -- that's a real, separate behavior, not a violation of
    this law, and mixing it in here would make the property false for a reason unrelated to what
    it's testing."""
    n = draw(st.integers(min_value=0, max_value=max_size))
    seqs = draw(st.lists(st.integers(min_value=0, max_value=max_size), min_size=n, max_size=n))
    out = []
    for seq in seqs:
        t = draw(st.sampled_from(ALL_TYPES))
        out.append({"seq": seq, "type": t, "payload": draw(_VALID_PAYLOAD[t])})
    return out


def _dedup_by_seq(evs):
    """Mirrors `fold`'s own first-occurrence-wins dedup, standalone -- so law 2's first half can
    compare against a value computed independently of the code under test."""
    seen, out = set(), []
    for ev in evs:
        seq = ev.get("seq")
        if isinstance(seq, int):
            if seq in seen:
                continue
            seen.add(seq)
        out.append(ev)
    return out


# --- Law 1: totality ---------------------------------------------------------

@settings(max_examples=MAX_EXAMPLES, deadline=None, derandomize=True)
@given(events_lists())
def test_law1_fold_never_raises(evs):
    """`fold` must not raise for ANY list of event dicts, however malformed -- the law with the
    widest blast radius (one corrupt event used to stop the whole dispatcher sweep, pre-RB-2).

    Teeth: replacing `_coerce_phase`'s `except ValueError: return default, ...` with a bare
    `Phase(raw).value` (no try/except) makes this fail on the very first `unknown_phase` example
    Hypothesis draws -- verified by hand while writing this test, not asserted from reading the
    code alone."""
    snap_mod.fold("K", evs)  # the assertion IS that this doesn't raise


# --- Law 2: duplicate-idempotence -------------------------------------------

@settings(max_examples=MAX_EXAMPLES, deadline=None, derandomize=True)
@given(_realistic_events())
def test_law2_fold_equals_fold_of_dedup_by_seq(evs):
    """fold(evs) == fold(dedup_by_seq(evs)).

    Teeth: deleting the dedup-by-seq loop at the top of `fold` (replaying `events` unfiltered)
    makes this fail as soon as Hypothesis draws two events sharing a seq -- which, with seqs
    drawn from a range as small as `max_size`, is nearly every example."""
    assert snap_mod.fold("K", evs).to_dict() == snap_mod.fold("K", _dedup_by_seq(evs)).to_dict()


@settings(max_examples=MAX_EXAMPLES, deadline=None, derandomize=True)
@given(_realistic_events())
def test_law2_fold_equals_fold_of_evs_times_two(evs):
    """fold(evs) == fold(evs * 2) -- the literal replay-the-whole-log-twice shape, including the
    counter-bearing event types (Failed -> failure_count, ReviewFeedbackReceived ->
    unresolved_reviews) that motivated this law in the first place (RB-2).

    Teeth: the same dedup-loop-deletion mutation as the test above breaks this one too, on the
    same first-colliding-seq example -- verified by hand. (`_realistic_events` only ever draws
    integer seqs, so a mutation narrowing the dedup guard's `isinstance(seq, int)` check would be
    a no-op against this generator specifically -- not a real distinguishing mutation here.)"""
    assert snap_mod.fold("K", evs).to_dict() == snap_mod.fold("K", evs * 2).to_dict()


# --- Law 3: observed_seq is a high-water mark -------------------------------

@settings(max_examples=MAX_EXAMPLES, deadline=None, derandomize=True)
@given(events_lists())
def test_law3_observed_seq_is_high_water_mark(evs):
    """observed_seq is monotone non-decreasing across any prefix, and equals max(seq) over the
    whole list -- an out-of-order tail can't move it backwards. Uses the general (malformed-
    inclusive) generator: observed_seq's computation only looks at `isinstance(seq, int)`, so it
    must hold regardless of how broken the rest of an event is.

    Teeth: changing `s.observed_seq = seq` (last-write, the pre-RB-2 bug) instead of `if seq >
    s.observed_seq: s.observed_seq = seq` (max) breaks the monotonicity assertion below on the
    first out-of-order example Hypothesis draws -- verified by hand."""
    prev = 0
    for i in range(len(evs) + 1):
        cur = snap_mod.fold("K", evs[:i]).observed_seq
        assert cur >= prev  # monotone non-decreasing across the prefix
        prev = cur
    # A fresh Snapshot's own observed_seq starts at 0 (event_log.append never assigns a seq <=
    # 0 -- seq = tail_seq + 1, tail_seq starting at 0 -- so 0 is a legitimate floor, not just an
    # artifact); the malformed generator can still draw a negative seq, which must not pull the
    # high-water mark backwards below that floor.
    expected = max([0, *(e.get("seq") for e in evs if isinstance(e.get("seq"), int))])
    assert snap_mod.fold("K", evs).observed_seq == expected  # equals max(0, *seqs), not last-write


# --- Law 4: phase closure, DONE absorbing -----------------------------------

@settings(max_examples=MAX_EXAMPLES, deadline=None, derandomize=True)
@given(events_lists())
def test_law4_phase_is_always_a_valid_phase(evs):
    """fold(evs).phase is always a value `Phase` accepts, for any input -- phase closure.

    Teeth: dropping `_coerce_phase`'s fallback (so an unrecognized phase string is stored
    verbatim instead of coerced) makes `Phase(snap.phase)` raise on the first `unknown_phase`
    example -- verified by hand."""
    snap = snap_mod.fold("K", evs)
    Phase(snap.phase)  # raises ValueError if `snap.phase` isn't a valid Phase


@settings(max_examples=MAX_EXAMPLES, deadline=None, derandomize=True)
@given(events_lists(max_size=8), events_lists(max_size=8))
# Hypothesis's generated draws alone found this weak: with only MAX_EXAMPLES=100 derandomized
# draws, none happened to land a *recognized* PhaseChanged phase (an unrecognized one is a
# no-op even with the guard removed -- `_coerce_phase` falls back to the snapshot's own current
# phase, which is already DONE) or a title-bearing TicketCreated into `suffix`, so a guard
# removed from just one of the three arms below could slip past undetected (found by RB-10's own
# QA loop). `@example` pins one deterministic case per guarded arm so each is ALWAYS exercised,
# regardless of what the random draws happen to land on.
#         seq=100, not 1: `prefix=[]` makes the synthesized Finalized event's seq exactly 1
#         (`max((), default=0) + 1`); a pinned example event sharing that seq would get silently
#         deduped away as a same-seq "duplicate" before it ever reaches the replay loop (found by
#         re-running this mutation by hand after `@example` alone appeared to pass against it).
@example(prefix=[], suffix=[{"seq": 100, "type": E.PHASE_CHANGED, "payload": {"phase": "ready"}}])
@example(prefix=[], suffix=[{"seq": 100, "type": E.TICKET_CREATED, "payload": {"title": "resurrected"}}])
@example(prefix=[], suffix=[{"seq": 100, "type": E.STALLED, "payload": {"reason": "r"}}])
def test_law4_done_is_absorbing(prefix, suffix):
    """Once folded to DONE, NO later event of ANY type (drawn from the full malformed
    generator, not just the three hand-picked types in test_snapshot.py) can move the phase
    again.

    Teeth: removing the `if s.phase == Phase.DONE.value: ...` guard from any ONE of the three
    arms that check it (TicketCreated/PhaseChanged/Stalled) makes this fail on the matching
    pinned `@example` above -- verified by hand for all three arms individually."""
    finalize_seq = max((e.get("seq") for e in prefix if isinstance(e.get("seq"), int)), default=0) + 1
    evs = prefix + [{"seq": finalize_seq, "type": E.FINALIZED, "payload": {}}] + suffix
    assert snap_mod.fold("K", evs).phase == Phase.DONE.value


# --- Regression tests for counterexamples found while writing the above -----
# Law 1 (totality) shrank out two real bugs during development:
#  1. `p = ev.get("payload") or {}` only guards a falsy payload (None/{}); a truthy non-dict
#     payload (e.g. `1`, `"x"`) sailed through into every `p.get(...)` call and raised
#     AttributeError. Fixed with an `isinstance(p, dict)` coercion, same posture as
#     `_coerce_phase`/`_coerce_turn`. Regression: test_snapshot.py::test_fold_is_total_non_dict_payload.
#  2. `_coerce_turn`'s `except (TypeError, ValueError)` didn't cover `int(float("inf"))`, which
#     raises `OverflowError` -- an infinite `turn` (produced by this file's own `bad_turn`
#     strategy) crashed `fold` instead of coercing. Fixed by adding `OverflowError` to the
#     except clause. Regression: test_snapshot.py::test_fold_is_total_infinite_turn.
# Both regressions live beside RB-2's other totality examples in test_snapshot.py, not
# duplicated here.
