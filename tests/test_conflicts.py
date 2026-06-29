"""PR merge-conflict handling: a CONFLICTING PR routes back to implementing so the
agent rebases/resolves/pushes (auto-resolution); ask_conflict is the human-escalation
fallback used only when the agent cannot resolve it itself."""
import time
from maestro import event_log, inbox, ops, snapshot as snap_mod, store
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
    """check-conflicts routes the ticket to implementing for auto-resolution — no human
    question on the happy path, so the PR can actually be updated by the agent."""
    from maestro.cli import main
    _create(cfg, "T-5")
    rc = main(["--home", str(cfg.home), "check-conflicts", "T-5", "42", "CONFLICTING"])
    assert rc == 0
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.IMPLEMENTING.value
    assert "conflict-T-5-42" not in snap.open_questions
    # the reason is carried on the PhaseChanged so the implementing reconcile knows why
    changed = [e for e in event_log.read(cfg.home, "T-5") if e["type"] == "PhaseChanged"
               and e["payload"].get("phase") == Phase.IMPLEMENTING.value]
    assert changed and "PR #42" in changed[-1]["payload"].get("reason", "")


def test_route_conflict_idempotent(cfg):
    """route_conflict moves awaiting-ci -> implementing once; a second call is a no-op."""
    _create(cfg, "T-5")  # starts in awaiting-ci
    assert ops.route_conflict(cfg, "T-5", 42) is True
    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IMPLEMENTING.value
    assert ops.route_conflict(cfg, "T-5", 42) is False  # already implementing
    impl_changes = [e for e in event_log.read(cfg.home, "T-5") if e["type"] == "PhaseChanged"
                    and e["payload"].get("phase") == Phase.IMPLEMENTING.value]
    assert len(impl_changes) == 1


def test_answered_questions_populated_after_fold(cfg):
    """fold_inbox moves answered qids into answered_questions on the snapshot."""
    _create(cfg, "T-5")
    ops.ask_conflict(cfg, "T-5", 42)
    inbox.append_command(cfg.home, "T-5", "ans",
                         {"text": "rebased and pushed", "qid": "conflict-T-5-42"})
    ops.fold_inbox(cfg, "T-5")
    snap = snap_mod.load(cfg.home, "T-5")
    assert "conflict-T-5-42" not in snap.open_questions
    assert "conflict-T-5-42" in snap.answered_questions
    assert snap.answered_questions["conflict-T-5-42"] == "rebased and pushed"


def test_answered_questions_cleared_on_phase_change(cfg):
    """PhaseChanged clears answered_questions so the next phase starts fresh."""
    _create(cfg, "T-5")
    ops.ask_conflict(cfg, "T-5", 42)
    inbox.append_command(cfg.home, "T-5", "ans",
                         {"text": "done", "qid": "conflict-T-5-42"})
    ops.fold_inbox(cfg, "T-5")
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.answered_questions  # populated

    ops.set_phase(cfg, "T-5", Phase.AWAITING_CI)
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.answered_questions == {}  # cleared


def test_conflict_resolution_full_flow(cfg):
    """Auto-resolution flow: CONFLICTING -> implementing (agent rebases & pushes) ->
    awaiting-ci. No human round-trip, and the PR-bearing branch gets updated."""
    from maestro.cli import main
    _create(cfg, "T-5")  # awaiting-ci with a PR open
    # Conflict detected from awaiting-ci → routed straight to implementing
    main(["--home", str(cfg.home), "check-conflicts", "T-5", "42", "CONFLICTING"])
    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IMPLEMENTING.value
    # The implementing reconcile rebases/resolves/pushes (real git work mocked here),
    # then returns to awaiting-ci to re-poll — no question was ever asked of the human.
    ops.set_phase(cfg, "T-5", Phase.AWAITING_CI, requeue_in=300)
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.AWAITING_CI.value
    assert not any(q.startswith("conflict-") for q in snap.open_questions)


def test_escalated_conflict_answer_retries_in_implementing(cfg):
    """When the agent escalates an unresolvable conflict (ask_conflict) and the human
    answers, the reconciler re-enters implementing to apply the guidance — not a bounce
    back to awaiting-ci that would just re-detect the same conflict."""
    _create(cfg, "T-5")
    ops.ask_conflict(cfg, "T-5", 42)  # escalation -> awaiting-human with a conflict- qid
    assert snap_mod.load(cfg.home, "T-5").phase == Phase.AWAITING_HUMAN.value
    inbox.append_command(cfg.home, "T-5", "ans",
                         {"text": "use theirs for that hunk", "qid": "conflict-T-5-42"})
    ops.fold_inbox(cfg, "T-5")
    snap = snap_mod.load(cfg.home, "T-5")
    assert [q for q in snap.answered_questions if q.startswith("conflict-")] == ["conflict-T-5-42"]
    # awaiting-human -> implementing is a valid transition (no 'unusual' note)
    seq_before = snap.observed_seq
    ops.set_phase(cfg, "T-5", Phase.IMPLEMENTING)
    inbox.ack(cfg.home, "T-5")
    evs = event_log.read(cfg.home, "T-5", since=seq_before)
    assert not [e for e in evs if e["type"] == "Note"
                and "unusual" in e.get("payload", {}).get("text", "")]
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.IMPLEMENTING.value
    assert snap.answered_questions == {}  # cleared on transition


def test_is_due_answered_pending_wakes_stuck_ticket(cfg):
    """A ticket in awaiting-human with answered_questions but no inbox_pending is due.
    This catches the crash-and-respawn case where fold ran but set-phase and inbox-ack
    didn't complete before the process died."""
    from maestro.dispatcher import is_due
    _create(cfg, "T-5")
    ops.ask_conflict(cfg, "T-5", 42)
    inbox.append_command(cfg.home, "T-5", "ans",
                         {"text": "done", "qid": "conflict-T-5-42"})
    ops.fold_inbox(cfg, "T-5")
    # Simulate crash: inbox was acked but set-phase was NOT called
    inbox.ack(cfg.home, "T-5")
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert snap.answered_questions  # populated by fold
    # Dispatcher must treat this as due even with inbox_pending=False
    result = is_due(snap, inbox_pending=False, current_spec_hash=None, now=time.time())
    assert result.due is True
    assert result.reason == "answered-pending"


def test_transitions_awaiting_human_to_awaiting_ci(cfg):
    """awaiting-human → awaiting-ci is now a valid transition (no NOTE event emitted)."""
    _create(cfg, "T-5")
    ops.ask_conflict(cfg, "T-5", 42)  # -> awaiting-human
    seq_before = snap_mod.load(cfg.home, "T-5").observed_seq
    ops.set_phase(cfg, "T-5", Phase.AWAITING_CI)
    evs = event_log.read(cfg.home, "T-5", since=seq_before)
    note_evs = [e for e in evs if e["type"] == "Note"
                and "unusual" in e.get("payload", {}).get("text", "")]
    assert note_evs == [], "No 'unusual transition' note should be emitted"
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.AWAITING_CI.value


def test_transitions_awaiting_ci_to_awaiting_human(cfg):
    """awaiting-ci → awaiting-human is now a valid transition (no NOTE event emitted)."""
    _create(cfg, "T-5")
    seq_before = snap_mod.load(cfg.home, "T-5").observed_seq
    ops.ask_conflict(cfg, "T-5", 42)  # awaiting-ci -> awaiting-human via ask_conflict
    evs = event_log.read(cfg.home, "T-5", since=seq_before)
    note_evs = [e for e in evs if e["type"] == "Note"
                and "unusual" in e.get("payload", {}).get("text", "")]
    assert note_evs == [], "No 'unusual transition' note for awaiting-ci -> awaiting-human"
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.AWAITING_HUMAN.value
