"""T-125 (a): the answer -> route label fold and `maestro scorecard answers`."""
import json

import pytest

from maestro import cli, decision_labels, event_log, inbox, ops, store
from maestro import dispatcher as disp
from maestro import snapshot as snap_mod
from maestro.dispatcher import dispatch_ledger_path
from maestro.idempotency import content_hash
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


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


def _create(cfg, key):
    """Mint a real ticket through the real verbs -- the T-122 fast-path
    eligibility checks (`dispatcher._answer_fast_path_eligible`) read the
    folded snapshot, so the AC2/AC4/AC5 tests below need a real one, not a
    raw-event stand-in."""
    home = cfg.home
    spec = f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n"
    store.atomic_write(store.spec_path(home, key), spec)
    payload = {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}
    event_log.append(home, key, "TicketCreated", payload, actor="d")
    snap_mod.rebuild(home, key)


def _shadow_round(cfg, key, question, answer, route_phase, route_reason):
    """One full round through the real verbs: ask, queue the human's answer,
    run a real (non-dry-run) sweep under `answer_fast_path = "shadow"` (which
    durably records its `would_route_answer` guess but changes no phase/inbox
    state), then drive the round's actual route by hand -- the same
    fold-inbox -> set-phase -> ack sequence the `awaiting-human` reconciler
    itself runs, with a caller-chosen destination/reason so a test can
    construct either an agreeing or a disagreeing round."""
    home = cfg.home
    ops.ask(cfg, key, question)
    inbox.append_command(home, key, "ans", {"text": answer})
    disp.dispatch(cfg, DryRunSessions(), now=store.now_epoch())
    ops.fold_inbox(cfg, key)
    snap = snap_mod.load(home, key)
    ops.set_phase(cfg, key, route_phase, reason=route_reason, expect=snap.observed_seq)
    inbox.ack(home, key)


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
    assert "agreement" not in out  # AC2: no fast-path decisions -> section omitted


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
    """The fold matches the round's actual route -- an automatic dispatcher
    reroute that ISN'T a T-122 fast-path approval (e.g. a review-feedback
    bounce) landing in the same window between an answer and the human-driven
    route must not be mistaken for that route."""
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
# T-139 AC1: round pairing -- ALL of a round's answers drain from the SAME
# route, through the real verbs (not raw events).
# ---------------------------------------------------------------------------

def test_round_pairing_drains_all_pending_answers_from_one_route(cfg):
    home = cfg.home
    key = "T-1"
    _create(cfg, key)

    ops.ask(cfg, key, "Question A?")
    ops.ask(cfg, key, "Question B?")
    inbox.append_command(home, key, "ans", {"text": "ok"})
    ops.fold_inbox(cfg, key)
    snap = snap_mod.load(home, key)
    ops.set_phase(cfg, key, Phase.READY, reason="approved: ok", expect=snap.observed_seq)
    snap2 = snap_mod.load(home, key)
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="worktree ready", expect=snap2.observed_seq)

    rc = cli.main(["--home", str(home), "scorecard", "answers"])
    assert rc == 0
    rows = store.read_jsonl(decision_labels.answers_path(home))
    assert len(rows) == 2
    for row in rows:
        assert row["label"] == "approve"
        assert row["phase_after"] == "ready"


# ---------------------------------------------------------------------------
# T-139 AC2: an "on"-mode dispatcher-actor route is recognized as a route,
# and a later ordinary reconciler transition neither adds nor relabels a row.
# ---------------------------------------------------------------------------

def test_on_mode_route_is_labeled_by_scorecard(cfg):
    home = cfg.home
    key = "T-1"
    _create(cfg, key)
    cfg.answer_fast_path = "on"
    qid = ops.ask(cfg, key, "Proceed?")
    inbox.append_command(home, key, "ans", {"text": "ok"})

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    rc = cli.main(["--home", str(home), "scorecard", "answers"])
    assert rc == 0
    rows = store.read_jsonl(decision_labels.answers_path(home))
    by_qid = {r["qid"]: r for r in rows}
    assert by_qid[qid]["label"] == "approve"
    assert by_qid[qid]["phase_after"] == "ready"

    snap = snap_mod.load(home, key)
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="worktree ready", expect=snap.observed_seq)

    rc = cli.main(["--home", str(home), "scorecard", "answers"])
    assert rc == 0
    rows_after = store.read_jsonl(decision_labels.answers_path(home))
    assert rows_after == rows


# ---------------------------------------------------------------------------
# AC2/T-139 AC4: agreement rate + consecutive streak, joined against durable
# T-122 FastPathDecided decisions written by real shadow sweeps.
# ---------------------------------------------------------------------------

def test_scorecard_agreement_and_streak_from_real_shadow_sweeps(cfg, capsys):
    home = cfg.home
    cfg.answer_fast_path = "shadow"
    for key in ("T-1", "T-2", "T-3"):
        _create(cfg, key)

    _shadow_round(cfg, "T-1", "Proceed?", "ok", Phase.READY, "approved: ok")        # agrees
    _shadow_round(cfg, "T-2", "Proceed?", "ok", Phase.TERMINATING, "rejected: no")  # disagrees
    _shadow_round(cfg, "T-3", "Proceed?", "ok", Phase.READY, "approved: ok")        # agrees

    rc = cli.main(["--home", str(home), "scorecard", "answers"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out["agreement"] == {
        "matched": 3, "agreements": 2,
        "agreement_rate": pytest.approx(2 / 3),
        "consecutive_streak": 1,
    }


def test_scorecard_agreement_absent_when_no_fast_path_decisions_recorded(cfg):
    home = cfg.home
    _round(home, "T-1", "q1", "ok", "approved: ok", phase="ready")

    rows = decision_labels.label_answers(home)
    assert decision_labels.agreement(rows, home) is None


def test_shadow_join_prints_real_agreement_after_a_sweep_and_real_route(cfg, capsys):
    """T-139 AC4: `answer_fast_path = "shadow"`, one asked question + a
    pending `ans ok`, a real sweep, then the reconciler's own real route --
    `agreement` must show one match and one agreement."""
    home = cfg.home
    key = "T-1"
    _create(cfg, key)
    cfg.answer_fast_path = "shadow"
    _shadow_round(cfg, key, "Proceed?", "ok", Phase.READY, "approved: ok")

    rc = cli.main(["--home", str(home), "scorecard", "answers"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["agreement"] == {
        "matched": 1, "agreements": 1,
        "agreement_rate": 1.0, "consecutive_streak": 1,
    }


def test_shadow_join_counts_disagreement_when_real_route_rejects(cfg, capsys):
    """Same flow, but the real route rejects instead of approving --
    `agreements` must be 0."""
    home = cfg.home
    key = "T-1"
    _create(cfg, key)
    cfg.answer_fast_path = "shadow"
    _shadow_round(cfg, key, "Proceed?", "ok", Phase.TERMINATING, "rejected: no")

    rc = cli.main(["--home", str(home), "scorecard", "answers"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["agreement"]["matched"] == 1
    assert out["agreement"]["agreements"] == 0


# ---------------------------------------------------------------------------
# T-139 AC5: durability (survives the 500-line ledger's trim/loss) + dedup
# (a repeated shadow sweep before the real route lands stays one decision).
# ---------------------------------------------------------------------------

def test_agreement_survives_ledger_loss_and_dedups_repeat_shadow_sweeps(cfg, capsys):
    home = cfg.home
    key = "T-1"
    _create(cfg, key)
    cfg.answer_fast_path = "shadow"
    ops.ask(cfg, key, "Proceed?")
    inbox.append_command(home, key, "ans", {"text": "ok"})

    disp.dispatch(cfg, DryRunSessions(), now=1000)
    disp.dispatch(cfg, DryRunSessions(), now=1001)  # repeat shadow sweep before routing

    fast_path_events = [e for e in event_log.read(home, key) if e["type"] == "FastPathDecided"]
    assert len(fast_path_events) == 1  # idempotent: one decision, not two

    ops.fold_inbox(cfg, key)
    snap = snap_mod.load(home, key)
    ops.set_phase(cfg, key, Phase.READY, reason="approved: ok", expect=snap.observed_seq)
    inbox.ack(home, key)

    # Simulate the dispatch ledger's own 500-line trim -- or its outright loss.
    ledger_path = dispatch_ledger_path(home)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("", encoding="utf-8")

    rc = cli.main(["--home", str(home), "scorecard", "answers"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["agreement"] == {
        "matched": 1, "agreements": 1,
        "agreement_rate": 1.0, "consecutive_streak": 1,
    }
