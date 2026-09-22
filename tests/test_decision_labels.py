"""T-125 (a): the answer -> route label fold and `maestro scorecard answers`."""
import json

import pytest

from maestro import cli, decision_labels, event_log, store
from maestro.dispatcher import dispatch_ledger_path
from maestro.idempotency import content_hash


def _round(home, key, qid, answer, reason, phase, *, question="Pick up this ticket -- OK?"):
    """One Q&A round, appended as raw events -- decision_labels only ever reads
    the log, so this bypasses ops/statemachine legality entirely and just
    produces the exact event shapes the fold matches against."""
    event_log.append(home, key, "QuestionAsked", {"qid": qid, "text": question},
                     actor="reconciler", step_id=f"ask-{key}-{qid}")
    event_log.append(home, key, "PhaseChanged", {"phase": "awaiting-human", "reason": "asked human"},
                     actor="reconciler")
    event_log.append(home, key, "QuestionAnswered", {"qid": qid, "answer": answer}, actor="human")
    event_log.append(home, key, "PhaseChanged", {"phase": phase, "reason": reason}, actor="reconciler")


def _ledger_line(qid, route, outcome="would_route_answer", key="T-1"):
    return json.dumps({
        "ts": store.iso_now(), "epoch": store.now_epoch(), "hook_errors": {},
        "decisions": {key: {"outcome": outcome, "qid": qid, "route": route}},
    }, separators=(",", ":"))


# ---------------------------------------------------------------------------
# AC1: labeling + counts by label
# ---------------------------------------------------------------------------

def test_scorecard_answers_labels_approve_and_reject_via_real_cli(cfg, capsys):
    home = cfg.home
    _round(home, "T-1", "q1", "ok", "approved: ok", phase="ready")
    _round(home, "T-2", "q2", "no thanks", "rejected: no thanks", phase="terminating")

    rc = cli.main(["--home", str(home), "scorecard", "answers"])
    assert rc == 0

    rows = store.read_jsonl(decision_labels.answers_path(home))
    assert len(rows) == 2
    by_qid = {r["qid"]: r for r in rows}
    assert by_qid["q1"] == {
        "key": "T-1", "qid": "q1", "qid_class": "named", "answer": "ok",
        "label": "approve", "phase_before": "awaiting-human", "phase_after": "ready",
        "ts": by_qid["q1"]["ts"],
    }
    assert by_qid["q2"]["label"] == "reject"
    assert by_qid["q2"]["answer"] == "no thanks"

    out = json.loads(capsys.readouterr().out)
    assert out["rows"] == 2
    assert out["by_label"] == {"approve": 1, "reject": 1}
    assert "agreement" not in out  # AC2: no ledger outcomes -> section omitted


def test_qid_class_content_hash_vs_named(cfg):
    home = cfg.home
    hashed_qid = content_hash("some question text")
    _round(home, "T-1", hashed_qid, "ok", "approved: ok", phase="ready")
    _round(home, "T-1", "conflict-T-1-9", "use approach B", "retry conflict resolution: use approach B",
          phase="implementing")

    rows = decision_labels.label_answers(home)
    by_qid = {r["qid"]: r for r in rows}
    assert by_qid[hashed_qid]["qid_class"] == "content_hash"
    assert by_qid["conflict-T-1-9"]["qid_class"] == "named"
    assert by_qid["conflict-T-1-9"]["label"] == "other"


def test_unrecognized_reason_is_unclassified_not_other(cfg):
    home = cfg.home
    _round(home, "T-1", "q1", "sure", "worktree ready", phase="implementing")

    rows = decision_labels.label_answers(home)
    assert rows[0]["label"] == "unclassified"


def test_dispatcher_actor_phase_change_is_skipped_over(cfg):
    """The fold matches the next NON-dispatcher PhaseChanged -- an automatic
    dispatcher reroute (e.g. a review-feedback bounce) landing in the same
    window between an answer and the human-driven route must not be mistaken
    for that route."""
    home = cfg.home
    event_log.append(home, "T-1", "QuestionAsked", {"qid": "q1", "text": "OK?"}, actor="reconciler")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": "awaiting-human", "reason": "asked human"},
                     actor="reconciler")
    event_log.append(home, "T-1", "QuestionAnswered", {"qid": "q1", "answer": "ok"}, actor="human")
    event_log.append(home, "T-1", "PhaseChanged",
                     {"phase": "implementing", "reason": "changes requested: unrelated bounce"},
                     actor="dispatcher")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": "ready", "reason": "approved: ok"},
                     actor="reconciler")

    rows = decision_labels.label_answers(home)
    assert len(rows) == 1
    assert rows[0]["label"] == "approve"
    assert rows[0]["phase_after"] == "ready"


def test_unanswered_trailing_question_is_omitted(cfg):
    home = cfg.home
    event_log.append(home, "T-1", "QuestionAsked", {"qid": "q1", "text": "OK?"}, actor="reconciler")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": "awaiting-human", "reason": "asked human"},
                     actor="reconciler")
    event_log.append(home, "T-1", "QuestionAnswered", {"qid": "q1", "answer": "ok"}, actor="human")
    # No route recorded yet -- nothing to label.

    assert decision_labels.label_answers(home) == []


def test_scorecard_regenerate_is_read_only(cfg):
    home = cfg.home
    _round(home, "T-1", "q1", "ok", "approved: ok", phase="ready")
    before = event_log.read(home, "T-1")

    decision_labels.regenerate(home)

    assert event_log.read(home, "T-1") == before


# ---------------------------------------------------------------------------
# AC2: agreement rate + consecutive streak against the T-122 ledger outcomes
# ---------------------------------------------------------------------------

def test_scorecard_agreement_and_streak_when_ledger_holds_outcomes(cfg, capsys):
    home = cfg.home
    _round(home, "T-1", "q1", "ok", "approved: ok", phase="ready")          # label approve
    _round(home, "T-1", "q2", "no", "rejected: no", phase="terminating")    # label reject
    _round(home, "T-1", "q3", "ok", "approved: ok", phase="ready")          # label approve

    ledger_path = dispatch_ledger_path(home)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        _ledger_line("q1", "approve") + "\n"      # agrees
        + _ledger_line("q2", "approve") + "\n"    # disagrees (actual: reject)
        + _ledger_line("q3", "approve") + "\n",   # agrees
        encoding="utf-8",
    )

    events_before = event_log.read(home, "T-1")
    rc = cli.main(["--home", str(home), "scorecard", "answers"])
    assert rc == 0
    assert event_log.read(home, "T-1") == events_before  # AC2: appends no event

    out = json.loads(capsys.readouterr().out)
    assert out["agreement"] == {
        "matched": 3, "agreements": 2,
        "agreement_rate": pytest.approx(2 / 3),
        "consecutive_streak": 1,
    }


def test_scorecard_agreement_absent_when_ledger_has_no_such_outcome(cfg):
    home = cfg.home
    _round(home, "T-1", "q1", "ok", "approved: ok", phase="ready")
    ledger_path = dispatch_ledger_path(home)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps({"ts": store.iso_now(), "decisions": {"T-1": {"outcome": "would_spawn"}}}) + "\n",
        encoding="utf-8",
    )

    rows = decision_labels.label_answers(home)
    assert decision_labels.agreement(rows, home) is None
