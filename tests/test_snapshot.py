from maestro import event_log, ops, snapshot as snap_mod
from maestro.statemachine import Phase


def test_fold_tracks_phase_and_seq(home):
    event_log.append(home, "T-1", "TicketCreated", {"title": "x", "spec_hash": "abc"}, actor="d")
    event_log.append(home, "T-1", "TicketTriaged", {"tier": "0", "phase": "ready"}, actor="r")
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.phase == Phase.READY.value
    assert snap.observed_seq == 2
    assert snap.title == "x"
    assert snap.spec_hash == "abc"


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
