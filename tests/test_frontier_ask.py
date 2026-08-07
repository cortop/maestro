"""T-24: frontier-round multi-question `maestro ask`.

The whole settled frontier goes out in ONE round -- N numbered questions,
each carrying an optional recommended answer -- via a repeatable `--question`
flag on the existing `ask` CLI verb, not a new event shape (`open_questions`
was already a qid-keyed dict). Proof obligation from the spec: a real-CLI test
that a multi-question round lands N entries in open_questions, that answering
a subset leaves the rest open, and that the reconciler is woken on the first
answer.
"""
from maestro import event_log, inbox, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.dispatcher import dispatch
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _create(home, key, *, tier=1):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: {tier}\n")
    event_log.append(home, key, "TicketCreated", {"title": key}, actor="d")
    snap_mod.rebuild(home, key)
    cli_main(["--home", str(home), "observe-spec", key])


def test_frontier_round_lands_n_entries_via_real_cli(cfg):
    """One `maestro ask` call with a repeatable --question flag posts N
    numbered questions -- each optionally carrying a recommendation -- into
    open_questions in a single round, with a single phase transition."""
    _create(cfg.home, "T-1")

    rc = cli_main([
        "--home", str(cfg.home), "ask", "T-1",
        "--question", "Use Postgres or SQLite?", "Postgres (matches prod)", "",
        "--question", "Cut a v2 API or extend v1?", "", "",
        "--question", "Who owns the migration script?", "the reconciler, via a sub-agent", "",
    ])
    assert rc == 0

    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert len(snap.open_questions) == 3

    texts = list(snap.open_questions.values())
    # Numbered 1/3..3/3, in the order passed.
    assert any(t.startswith("1/3. Use Postgres or SQLite?") for t in texts)
    assert any(t.startswith("2/3. Cut a v2 API or extend v1?") for t in texts)
    assert any(t.startswith("3/3. Who owns the migration script?") for t in texts)
    # Recommendations are attached; the question with no recommendation carries none.
    assert any("Recommended: Postgres (matches prod)" in t for t in texts)
    assert any("Recommended: the reconciler, via a sub-agent" in t for t in texts)
    assert not any("Cut a v2 API" in t and "Recommended:" in t for t in texts)

    # Exactly one PhaseChanged for the whole round, not one per question.
    evs = event_log.read(cfg.home, "T-1")
    asked = [e for e in evs if e["type"] == "QuestionAsked"]
    phase_changes = [e for e in evs if e["type"] == "PhaseChanged"]
    assert len(asked) == 3
    assert len(phase_changes) == 1


def test_answering_a_subset_leaves_the_rest_open(cfg):
    _create(cfg.home, "T-2")
    cli_main([
        "--home", str(cfg.home), "ask", "T-2",
        "--question", "Question A?", "", "",
        "--question", "Question B?", "", "",
    ])
    snap = snap_mod.load(cfg.home, "T-2")
    assert len(snap.open_questions) == 2
    qid_a, qid_b = list(snap.open_questions.keys())

    rc = cli_main(["--home", str(cfg.home), "ans", "T-2", "yes to A", "--qid", qid_a, "--no-nudge"])
    assert rc == 0
    cli_main(["--home", str(cfg.home), "fold-inbox", "T-2"])

    snap = snap_mod.load(cfg.home, "T-2")
    assert qid_a not in snap.open_questions
    assert qid_b in snap.open_questions
    assert snap.answered_questions.get(qid_a) == "yes to A"
    assert qid_b not in snap.answered_questions


def test_reconciler_wakes_on_first_answer(cfg):
    """Answering just one question of a still-open multi-question round is
    enough to make the ticket due again on the very next dispatcher sweep --
    the human does not have to clear the whole frontier first."""
    _create(cfg.home, "T-3")
    cli_main([
        "--home", str(cfg.home), "ask", "T-3",
        "--question", "Question A?", "", "",
        "--question", "Question B?", "", "",
    ])
    snap = snap_mod.load(cfg.home, "T-3")
    qid_a, qid_b = list(snap.open_questions.keys())

    # Nothing pending yet -- not due.
    report = dispatch(cfg, DryRunSessions(), now=1000)
    assert "T-3" not in dict(report.due)

    # Answer only the first question; the second is still unanswered.
    cli_main(["--home", str(cfg.home), "ans", "T-3", "yes to A", "--qid", qid_a, "--no-nudge"])

    report = dispatch(cfg, DryRunSessions(), now=1001)
    assert dict(report.due).get("T-3") == "inbox"
    assert "T-3" in report.spawned

    # Simulate that spawned reconciler's one step (DryRunSessions doesn't run
    # the real reconciler): fold the pending answer and confirm the subset
    # that was actually answered is what clears.
    cli_main(["--home", str(cfg.home), "fold-inbox", "T-3"])
    snap = snap_mod.load(cfg.home, "T-3")
    assert qid_b in snap.open_questions
    assert qid_a not in snap.open_questions


def test_single_question_mode_still_works(cfg):
    """Backward compat: the old single positional-text form (used by
    ask_conflict and friends) is untouched."""
    _create(cfg.home, "T-4")
    rc = cli_main(["--home", str(cfg.home), "ask", "T-4", "Plain single question?", "--qid", "q1"])
    assert rc == 0
    snap = snap_mod.load(cfg.home, "T-4")
    assert snap.open_questions == {"q1": "Plain single question?"}


def test_text_and_question_flags_are_mutually_exclusive(cfg):
    _create(cfg.home, "T-5")
    rc = cli_main([
        "--home", str(cfg.home), "ask", "T-5", "some text",
        "--question", "Q?", "", "",
    ])
    assert rc == 2


def test_explicit_qid_in_a_round_survives_for_prefix_routing(cfg):
    """A round can still pin one question's qid (e.g. the research-approval-<key>
    convention a later reconcile step routes on by prefix) while leaving the
    rest auto-derived."""
    _create(cfg.home, "T-6")
    cli_main([
        "--home", str(cfg.home), "ask", "T-6",
        "--question", "Approve the proposal?", "approve", "research-approval-T-6",
        "--question", "Also widen scope to Y?", "no, keep it minimal", "",
    ])
    snap = snap_mod.load(cfg.home, "T-6")
    assert "research-approval-T-6" in snap.open_questions
    assert len(snap.open_questions) == 2
