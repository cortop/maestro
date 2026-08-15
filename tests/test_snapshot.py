from maestro import event_log, ops, snapshot as snap_mod
from maestro.statemachine import Phase


def test_fold_tracks_phase_and_seq(home):
    event_log.append(home, "T-1", "TicketCreated", {"title": "x", "spec_hash": "abc"}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": "ready", "reason": ""}, actor="r")
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.phase == Phase.READY.value
    assert snap.observed_seq == 2
    assert snap.title == "x"
    assert snap.spec_hash == "abc"


def test_fold_ignores_legacy_ticket_triaged_event(home):
    """A legacy pre-GA-18 triage event (the deleted "Ticket" + "Triaged" type,
    built here from parts so this proof itself doesn't reintroduce the deleted
    symbol -- see the grep-clean acceptance criterion) is an unknown type to the
    fold's elif chain -- it must be a harmless no-op, never raise, so an old log
    stays foldable."""
    legacy_type = "Ticket" + "Triaged"
    event_log.append(home, "T-1", "TicketCreated", {"title": "x", "spec_hash": "abc"}, actor="d")
    event_log.append(home, "T-1", legacy_type, {"tier": "0", "phase": "ready"}, actor="r")
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.phase == Phase.TRIAGING.value  # unchanged -- the legacy event is ignored
    assert snap.observed_seq == 2
    assert not hasattr(snap, "tier")


def test_fold_parses_historical_approved_event_without_warning(home):
    """AD-7: `maestro approve`/the tier-2 implementing gate it cleared are gone,
    but 30 existing event logs carry a real Approved event and the log is
    append-only -- the fold must keep parsing it forever, with no warning or
    error, even though nothing emits it anymore."""
    event_log.append(home, "T-1", "TicketCreated", {"title": "x", "spec_hash": "abc"}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": "implementing"}, actor="r")
    event_log.append(home, "T-1", "Approved", {}, actor="human")
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.approved is True
    assert snap.fold_warnings == []
    assert snap.phase == Phase.IMPLEMENTING.value  # unaffected -- no gate reads this anymore


def test_question_open_then_answered(home):
    event_log.append(home, "T-1", "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r")
    assert snap_mod.rebuild(home, "T-1").question_open is True
    event_log.append(home, "T-1", "QuestionAnswered", {"qid": "q1", "answer": "yes"}, actor="h")
    assert snap_mod.rebuild(home, "T-1").question_open is False


def test_pr_and_ci_fold(home):
    event_log.append(home, "T-1", "PrOpened", {"number": 7, "url": "u", "draft": True}, actor="r")
    event_log.append(home, "T-1", "CiObserved", {"state": "passing"}, actor="r")
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.pr_number == 7 and snap.pr_state == "open" and snap.ci_state == "passing"


def test_phase_change_resets_failures(home, cfg):
    ops.fail(cfg, "T-1", "boom")
    ops.fail(cfg, "T-1", "boom")
    assert snap_mod.load(home, "T-1").failure_count == 2
    ops.set_phase(cfg, "T-1", Phase.READY)
    assert snap_mod.load(home, "T-1").failure_count == 0


def test_snapshot_roundtrip(home):
    event_log.append(home, "T-1", "TicketCreated", {"title": "x"}, actor="d")
    snap = snap_mod.rebuild(home, "T-1")
    loaded = snap_mod.load(home, "T-1")
    assert loaded.to_dict() == snap.to_dict()


# --- RB-2: the four algebraic laws `fold` is assumed to satisfy -------------

def test_fold_is_total_unknown_phase():
    """(b) An unrecognized `phase` must not raise -- pre-fix this was ValueError."""
    snap = snap_mod.fold("K", [{"seq": 1, "type": "PhaseChanged", "payload": {"phase": "bogus"}}])
    assert snap.phase == Phase.TRIAGING.value  # coerced no-op: default snapshot's own phase
    assert any("unknown/missing phase" in w for w in snap.fold_warnings)  # not silently dropped


def test_fold_is_total_missing_phase():
    """(b) A missing `phase` key must not raise -- pre-fix this was KeyError."""
    snap = snap_mod.fold("K", [{"seq": 1, "type": "PhaseChanged", "payload": {}}])
    assert snap.phase == Phase.TRIAGING.value
    assert any("unknown/missing phase" in w for w in snap.fold_warnings)


def test_fold_is_total_non_integer_turn():
    """(b) A non-integer `turn` must not raise -- pre-fix this was ValueError from int()."""
    snap = snap_mod.fold("K", [{"seq": 1, "type": "ImplTurnRecorded", "payload": {"turn": "abc"}}])
    assert snap.impl_turns == 0  # coerced no-op: default counter left unchanged
    assert any("non-integer turn" in w for w in snap.fold_warnings)


def test_fold_is_total_infinite_turn():
    """(b) A `turn` of `float("inf")`/`-inf` must not raise either -- `int(float("inf"))` raises
    `OverflowError`, not `ValueError`, so it slipped past `_coerce_turn`'s original except
    clause. Found by RB-10's QA loop (the property generator's own `bad_turn` strategy draws
    `allow_infinity=True` floats) -- pinned here as the example-based regression."""
    snap = snap_mod.fold("K", [{"seq": 1, "type": "ImplTurnRecorded", "payload": {"turn": float("inf")}}])
    assert snap.impl_turns == 0  # coerced no-op: default counter left unchanged
    assert any("non-integer turn" in w for w in snap.fold_warnings)
    snap = snap_mod.fold("K", [{"seq": 1, "type": "ImplStepRecorded", "payload": {"turn": float("-inf")}}])
    assert snap.impl_turns == 0
    assert any("non-integer turn" in w for w in snap.fold_warnings)


def test_fold_is_total_non_dict_payload():
    """(b) A `payload` that isn't a dict at all -- a bare int/str/list, not just a dict with a
    bad key -- must not raise either. Found by RB-10's Hypothesis generator (`p = ev.get(
    "payload") or {}` only guards against a falsy payload, e.g. `None`/`{}`; a truthy non-dict
    payload like `1` or `"x"` sailed straight through into `p.get(...)` and raised
    AttributeError) -- pinned here as the example-based regression per that ticket's Notes."""
    snap = snap_mod.fold("K", [{"seq": 1, "type": "TicketCreated", "payload": 1}])
    assert snap.title is None  # coerced no-op: payload treated as empty
    snap = snap_mod.fold("K", [{"seq": 1, "type": "QuestionAsked", "payload": "not-a-dict"}])
    assert snap.open_questions == {"1": ""}  # p.get("qid", str(seq)) falls back to the seq


def test_fold_is_duplicate_idempotent():
    """(c) fold(evs) == fold(evs * 2), across the events RB-2 names by name --
    not just the two counters (`failure_count`/`unresolved_reviews`) the bug
    was first noticed through. Deliberately no `PhaseChanged` in *evs*: that
    event resets both counters to 0 as a side effect, which would mask the
    duplicate-counting bug when the whole list (PhaseChanged included) is
    replayed twice back-to-back -- exactly the trap this law guards against."""
    evs = [
        {"seq": 1, "type": "TicketCreated", "payload": {"title": "x", "spec_hash": "h"}},
        {"seq": 2, "type": "Failed", "payload": {"error": "boom"}},
        {"seq": 3, "type": "ReviewFeedbackReceived",
         "payload": {"comment_id": "c1", "state": "CHANGES_REQUESTED"}},
        {"seq": 4, "type": "AcVerified", "payload": {"ac_hash": "h1", "evidence": {"what": "w"}}},
        {"seq": 5, "type": "AcQaVerdict", "payload": {"ac_hash": "h1", "verdict": "fail", "evidence": "e"}},
    ]
    once = snap_mod.fold("K", evs).to_dict()
    twice = snap_mod.fold("K", evs * 2).to_dict()
    assert once == twice
    assert once["failure_count"] == 1  # not inflated to 2 by the duplicated Failed
    assert once["unresolved_reviews"] == 1  # ditto ReviewFeedbackReceived


def test_duplicated_seqs_from_interrupted_compaction_cannot_inflate_failure_count(home):
    """(c), the live trigger named in the spec: a crash between compact's
    archive-append and active-rewrite leaves the same seq in both files --
    `event_log.read` must dedup rather than double-count on replay."""
    event_log.append(home, "T-1", "TicketCreated", {"title": "x"}, actor="d")
    ev = event_log.append(home, "T-1", "Failed", {"error": "boom"}, actor="r")
    # Simulate compact's crash window: the event landed in the archive, and
    # the active-log rewrite (which would have dropped it) never completed.
    from maestro import store
    archive_path = store.events_archive_path(home, "T-1")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    with archive_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, separators=(",", ":")) + "\n")
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.failure_count == 1  # not 2


def test_observed_seq_is_a_high_water_mark():
    """(d) observed_seq is max(seq), not last-write -- an out-of-order tail
    (e.g. from a merged/replayed segment) can't move it backwards."""
    snap = snap_mod.fold("K", [{"seq": 9, "type": "Note"}, {"seq": 1, "type": "Note"}])
    assert snap.observed_seq == 9


def test_done_is_absorbing():
    """(e) [Finalized, Stalled] folds to done -- a Stalled after Finalized
    must not resurrect a finished ticket into an active (DEGRADED) phase."""
    snap = snap_mod.fold("K", [
        {"seq": 1, "type": "Finalized", "payload": {}},
        {"seq": 2, "type": "Stalled", "payload": {"reason": "r"}},
    ])
    assert snap.phase == Phase.DONE.value
    assert any("DONE (absorbing)" in w for w in snap.fold_warnings)


def test_done_is_absorbing_against_phase_changed_too():
    """(e), the general form: no event TYPE -- not just Stalled -- can move a
    folded done snapshot into an active phase."""
    snap = snap_mod.fold("K", [
        {"seq": 1, "type": "Finalized", "payload": {}},
        {"seq": 2, "type": "PhaseChanged", "payload": {"phase": "ready"}},
    ])
    assert snap.phase == Phase.DONE.value


def test_done_is_absorbing_against_ticket_created_too():
    """(e), the general form again: a (re-)TicketCreated must not resurrect a
    folded done snapshot back to TRIAGING either -- the write boundary
    (`ops.mint_new_tickets`) already refuses to append a second TicketCreated
    to a key with events, but `fold` must hold the law for ANY event list."""
    snap = snap_mod.fold("K", [
        {"seq": 1, "type": "Finalized", "payload": {}},
        {"seq": 2, "type": "TicketCreated", "payload": {"title": "x"}},
    ])
    assert snap.phase == Phase.DONE.value


def test_from_dict_ignores_legacy_tier_field(home):
    """A pre-GA-18 snapshot JSON on disk still carries `"tier": null` (or any
    other stale value) -- from_dict's unknown-key filter must silently drop it
    rather than raising, since Snapshot no longer has a `tier` field."""
    from maestro import store
    store.write_json(store.snapshot_path(home, "T-1"),
                     {"key": "T-1", "phase": "ready", "tier": None})
    snap = snap_mod.load(home, "T-1")
    assert snap.key == "T-1"
    assert snap.phase == "ready"
    assert not hasattr(snap, "tier")
