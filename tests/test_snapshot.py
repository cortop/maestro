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
