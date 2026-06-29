"""PR merge-conflict detection: ask_conflict is idempotent and skips if already open."""
from maestro import event_log, ops, snapshot as snap_mod, store
from maestro.statemachine import Phase


def _create(cfg, key):
    store.atomic_write(store.spec_path(cfg.home, key), f"# {key}\napproval_tier: 1\n")
    event_log.append(cfg.home, key, "TicketCreated", {"title": key}, actor="d")
    snap_mod.rebuild(cfg.home, key)
    ops.set_phase(cfg, key, Phase.AWAITING_CI)


def test_ask_conflict_emits_question(cfg):
    _create(cfg, "T-5")
    asked = ops.ask_conflict(cfg, "T-5", 42)
    assert asked is True
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    qid = "conflict-T-5-42"
    assert qid in snap.open_questions
    assert "42" in snap.open_questions[qid]
    assert "conflict" in snap.open_questions[qid].lower()


def test_ask_conflict_skips_if_already_open(cfg):
    _create(cfg, "T-5")
    first = ops.ask_conflict(cfg, "T-5", 42)
    assert first is True
    second = ops.ask_conflict(cfg, "T-5", 42)
    assert second is False
    evs = event_log.read(cfg.home, "T-5")
    asked_evs = [e for e in evs if e["type"] == "QuestionAsked"]
    assert len(asked_evs) == 1


def test_ask_conflict_uses_deterministic_qid(cfg):
    _create(cfg, "T-5")
    ops.ask_conflict(cfg, "T-5", 99)
    snap = snap_mod.load(cfg.home, "T-5")
    assert "conflict-T-5-99" in snap.open_questions


def test_ask_conflict_no_op_for_different_pr(cfg):
    """Two different PR numbers produce independent (non-blocking) questions."""
    _create(cfg, "T-5")
    ops.ask_conflict(cfg, "T-5", 10)
    # After answering the first conflict question the phase returns to awaiting-ci
    from maestro import inbox
    inbox.append_command(cfg.home, "T-5", "ans", {"text": "rebased", "qid": "conflict-T-5-10"})
    ops.fold_inbox(cfg, "T-5")
    ops.set_phase(cfg, "T-5", Phase.AWAITING_CI)
    inbox.ack(cfg.home, "T-5")

    asked = ops.ask_conflict(cfg, "T-5", 11)
    assert asked is True
    snap = snap_mod.load(cfg.home, "T-5")
    assert "conflict-T-5-11" in snap.open_questions


def test_check_conflicts_cli_non_conflicting(cfg, tmp_path):
    """check-conflicts is a no-op when state != CONFLICTING."""
    from maestro.cli import main
    _create(cfg, "T-5")
    rc = main(["--home", str(cfg.home), "check-conflicts", "T-5", "99", "MERGEABLE"])
    assert rc == 0
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.AWAITING_CI.value


def test_check_conflicts_cli_conflicting(cfg):
    """check-conflicts emits a question when state == CONFLICTING."""
    from maestro.cli import main
    _create(cfg, "T-5")
    rc = main(["--home", str(cfg.home), "check-conflicts", "T-5", "42", "CONFLICTING"])
    assert rc == 0
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert "conflict-T-5-42" in snap.open_questions
