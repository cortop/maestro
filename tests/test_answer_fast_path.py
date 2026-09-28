"""T-122: the dispatcher's due loop routing an exact-literal human approval
straight to `ready` itself, in the same sweep, instead of spawning a full
`awaiting-human` reconciler whose only job in most cases is to read "ok" and
call `set-phase ready`. Drives a real `dispatch(cfg, DryRunSessions(), ...)`
sweep over a temp home; mocks nothing but the spawn.
"""
import json

import pytest

from maestro import config as config_mod, diagram
from maestro import dispatcher as disp
from maestro import event_log, inbox, ops, projection, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.idempotency import content_hash
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _create(cfg, key, *, deps=None, kind=None):
    deps_line = f"dependsOn: [{', '.join(deps)}]\n" if deps is not None else ""
    spec = f"# {key}\n{deps_line}\n## Acceptance criteria\n- [ ] ok\n"
    store.atomic_write(store.spec_path(cfg.home, key), spec)
    payload = {"title": key, "spec_hash": disp.spec_hash_on_disk(cfg.home, key)}
    if kind:
        payload["kind"] = kind
    event_log.append(cfg.home, key, "TicketCreated", payload, actor="d")
    snap_mod.rebuild(cfg.home, key)


def _dispatcher_phase_changes(cfg, key):
    return [e for e in event_log.read(cfg.home, key)
            if e["type"] == "PhaseChanged" and e["actor"] == "dispatcher"]


def _ledger_decisions(cfg):
    lines = disp.dispatch_ledger_path(cfg.home).read_text().splitlines()
    return json.loads(lines[-1])["decisions"]


# --- AC1: unset knob is byte-identical to before this ticket ---------------

def test_off_is_byte_identical_to_todays_sweep(cfg):
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert _ledger_decisions(cfg)["T-1"]["outcome"] == "spawned"
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert inbox.pending(cfg.home, "T-1")          # never folded/acked
    assert not _dispatcher_phase_changes(cfg, "T-1")


# --- AC2: shadow records would_route_answer but still spawns ---------------

def test_shadow_records_would_route_answer_and_still_spawns(cfg):
    cfg.answer_fast_path = "shadow"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert _ledger_decisions(cfg)["T-1"] == {
        "outcome": "would_route_answer", "reason": "would approve: ok",
        "qid": [content_hash("Proceed?")], "route": "approve"}
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert inbox.pending(cfg.home, "T-1")
    assert not _dispatcher_phase_changes(cfg, "T-1")


def test_on_under_dry_run_previews_like_shadow(cfg):
    """GA-4: dry_run is strictly read-only -- `on` must not fold/route/ack
    under a preview sweep, same posture as every other dry_run hook."""
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "ok"})

    disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)

    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert inbox.pending(cfg.home, "T-1")
    assert not _dispatcher_phase_changes(cfg, "T-1")


# --- AC3: on folds + routes to ready and spawns it the same sweep ----------

def test_on_routes_literal_ok_to_ready_same_sweep(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")     # auto content-hash qid
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.READY.value
    changes = _dispatcher_phase_changes(cfg, "T-1")
    assert len(changes) == 1
    assert changes[0]["payload"]["phase"] == "ready"
    assert changes[0]["payload"]["reason"] == "approved: ok"
    assert not inbox.pending(cfg.home, "T-1")           # folded + acked
    # No awaiting-human reconciler spawned for the fast-pathed answer --
    # the `ready` reconciler spawns instead, in this same sweep.
    assert report.spawned == ["T-1"]
    assert _ledger_decisions(cfg)["T-1"] == {
        "outcome": "answer_routed", "reason": "approved: ok",
        "qid": [content_hash("Proceed?")], "route": "approve"}


def test_on_uses_the_verbatim_answer_case(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "Yes"})

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    changes = _dispatcher_phase_changes(cfg, "T-1")
    assert changes[0]["payload"]["reason"] == "approved: Yes"


def test_on_records_answer_routed_when_dependency_still_blocks_ready(cfg):
    """The route itself still happens (phase -> ready), but the just-routed
    ticket isn't immediately due (blocked-dep) -- so it never re-enters the
    spawn loop this sweep, and `answer_routed` is the ledger's final word."""
    cfg.answer_fast_path = "on"
    _create(cfg, "T-dep")               # left in triaging: not done
    _create(cfg, "T-1", deps=["T-dep"])
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert _ledger_decisions(cfg)["T-1"] == {
        "outcome": "answer_routed", "reason": "approved: ok",
        "qid": [content_hash("Proceed?")], "route": "approve"}
    assert "T-1" not in report.spawned
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.READY.value


# --- AC4: every other shape falls through to today's spawn -----------------

def test_non_literal_answer_falls_through(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "let me think about it"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert inbox.pending(cfg.home, "T-1")
    assert not _dispatcher_phase_changes(cfg, "T-1")


def test_research_kind_falls_through(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1", kind="research")
    ops.ask(cfg, "T-1", "Approve this research proposal?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert not _dispatcher_phase_changes(cfg, "T-1")


def test_named_qid_falls_through(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Sensitive file touched, proceed?", qid="blocked-sensitive-file-T-1")
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"text": "ok", "qid": "blocked-sensitive-file-T-1"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert not _dispatcher_phase_changes(cfg, "T-1")


def test_partially_answered_frontier_round_falls_through(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask_round(cfg, "T-1", [("Question A?", None, None), ("Question B?", None, None)])
    snap = snap_mod.load(cfg.home, "T-1")
    qid_a = list(snap.open_questions.keys())[0]
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "ok", "qid": qid_a})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert not _dispatcher_phase_changes(cfg, "T-1")


def test_two_pending_answers_for_one_key_falls_through(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "ok"})
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert not _dispatcher_phase_changes(cfg, "T-1")


# --- AC5: idempotent re-sweep; unknown value fails config.load() closed ----

def test_second_sweep_after_route_appends_nothing_new(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "ok"})

    disp.dispatch(cfg, DryRunSessions(), now=1000)
    first_changes = _dispatcher_phase_changes(cfg, "T-1")
    assert len(first_changes) == 1

    disp.dispatch(cfg, DryRunSessions(), now=1001)
    second_changes = _dispatcher_phase_changes(cfg, "T-1")
    assert second_changes == first_changes


def test_unknown_answer_fast_path_value_fails_closed(home):
    (home / "config.toml").write_text('[maestro]\nanswer_fast_path = "sometimes"\n',
                                      encoding="utf-8")
    with pytest.raises(store.MaestroError) as exc:
        config_mod.load(str(home))
    msg = str(exc.value)
    assert "answer_fast_path" in msg and "sometimes" in msg
    for valid in ("off", "shadow", "on"):
        assert valid in msg


# --- AC6: outcome bookkeeping + generated docs + NEEDS-YOU visibility ------

def test_new_outcomes_absent_from_silent_skip_outcomes():
    assert "answer_routed" not in disp._SILENT_SKIP_OUTCOMES
    assert "would_route_answer" not in disp._SILENT_SKIP_OUTCOMES


def test_new_outcomes_appear_in_generated_dispatch_gates_doc():
    generated = diagram.render_dispatch_gates()
    assert "`answer_routed`" in generated
    assert "`would_route_answer`" in generated
    assert generated == diagram.DISPATCH_GATES_PATH.read_text()


def test_needs_you_lists_fast_pathed_ticket_with_undo_hint(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "ok"})
    disp.dispatch(cfg, DryRunSessions(), now=1000)

    docs = projection.render(cfg.home)
    needs_you = docs["NEEDS-YOU.md"]
    assert "## Routed without a reconciler (24h)" in needs_you
    assert "T-1" in needs_you
    assert "approved: ok" in needs_you
    assert "maestro cmd T-1 discard" in needs_you


# ============================================================================
# T-140: widen the fast path to accepted recommendations (marker/echo), short
# unqualified approvals, and multi-question rounds.
# ============================================================================

def _ask_with_recommend(cfg, key, question, recommend, kind, *, qid=None):
    return ops.ask_round(cfg, key, [(question, recommend, qid, kind)])[0]


def _fast_path_decided(cfg, key):
    return [e for e in event_log.read(cfg.home, key) if e["type"] == "FastPathDecided"]


# --- AC2: recommend_kind declared via the CLI, persists through the fold ---

def test_ask_question_records_recommend_kind_and_survives_fold(cfg):
    _create(cfg, "T-1")
    rc = cli_main(["--home", str(cfg.home), "ask", "T-1",
                   "--question", "Proceed?", "Yes, proceed", "", "proceed"])
    assert rc == 0

    snap = snap_mod.load(cfg.home, "T-1")
    qid = next(iter(snap.open_questions))
    assert snap.question_kinds[qid] == "proceed"
    assert snap.question_recommends[qid] == "Yes, proceed"

    inbox.append_command(cfg.home, "T-1", "ans", {"qid": qid, "text": "Yes, proceed"})
    ops.fold_inbox(cfg, "T-1")
    snap2 = snap_mod.load(cfg.home, "T-1")
    assert snap2.question_kinds[qid] == "proceed"          # survives the fold
    assert snap2.answered_questions[qid] == "Yes, proceed"


def test_ask_question_three_args_records_no_kind(cfg):
    _create(cfg, "T-1")
    rc = cli_main(["--home", str(cfg.home), "ask", "T-1",
                   "--question", "Proceed?", "Yes, proceed", ""])
    assert rc == 0

    snap = snap_mod.load(cfg.home, "T-1")
    qid = next(iter(snap.open_questions))
    assert qid not in snap.question_kinds
    assert snap.question_recommends[qid] == "Yes, proceed"


def test_ask_question_rejects_unrecognized_kind(cfg):
    _create(cfg, "T-1")
    with pytest.raises(store.MaestroError, match="proceed"):
        ops.ask_round(cfg, "T-1", [("Proceed?", "Yes", None, "sometimes")])


# --- AC3: single-question fast path widened (marker, echo, prefix) ---------

def test_on_routes_marker_accept_of_proceed_recommendation(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    qid = _ask_with_recommend(cfg, "T-1", "Proceed?", "Yes, proceed with the plan", "proceed")
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qid, "text": "Yes, proceed with the plan",
                          "accepted_recommendation": True})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.READY.value
    changes = _dispatcher_phase_changes(cfg, "T-1")
    assert changes[0]["payload"]["reason"] == "approved: Yes, proceed with the plan"
    assert report.spawned == ["T-1"]
    assert _fast_path_decided(cfg, "T-1")[-1]["payload"]["rules"] == {qid: "marker"}


def test_on_routes_typed_echo_differing_in_case_whitespace_punctuation(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    qid = _ask_with_recommend(cfg, "T-1", "Proceed?", "Yes, proceed with the plan", "proceed")
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qid, "text": "  yes, proceed WITH the plan.  "})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.READY.value
    assert report.spawned == ["T-1"]
    assert _fast_path_decided(cfg, "T-1")[-1]["payload"]["rules"] == {qid: "echo"}


def test_on_routes_prefix_answer_with_no_recommendation(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "yes proceed"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.READY.value
    changes = _dispatcher_phase_changes(cfg, "T-1")
    assert changes[0]["payload"]["reason"] == "approved: yes proceed"
    assert report.spawned == ["T-1"]


# --- AC4: multi-question rounds route once, with every verbatim answer -----

def test_on_routes_two_question_round(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    qids = ops.ask_round(cfg, "T-1", [
        ("Use Postgres or SQLite?", "Postgres", None, "proceed"),
        ("Cut a v2 API or extend v1?", None, None, None),
    ])
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qids[0], "text": "Postgres", "accepted_recommendation": True})
    inbox.append_command(cfg.home, "T-1", "ans", {"qid": qids[1], "text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.READY.value
    changes = _dispatcher_phase_changes(cfg, "T-1")
    assert len(changes) == 1
    reason = changes[0]["payload"]["reason"]
    assert reason.startswith("approved: ") and "Postgres" in reason and "ok" in reason
    assert report.spawned == ["T-1"]


def test_on_routes_three_question_round_mixed(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    qids = ops.ask_round(cfg, "T-1", [
        ("Use Postgres or SQLite?", "Postgres", None, "proceed"),
        ("Who owns the migration?", "the reconciler", None, "proceed"),
        ("Cut a v2 API or extend v1?", None, None, None),
    ])
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qids[0], "text": "Postgres", "accepted_recommendation": True})
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qids[1], "text": "the reconciler", "accepted_recommendation": True})
    inbox.append_command(cfg.home, "T-1", "ans", {"qid": qids[2], "text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.READY.value
    assert len(_dispatcher_phase_changes(cfg, "T-1")) == 1
    assert report.spawned == ["T-1"]
    assert _fast_path_decided(cfg, "T-1")[-1]["payload"]["rules"] == {
        qids[0]: "marker", qids[1]: "marker", qids[2]: "literal"}


def test_round_falls_through_when_one_answer_unqualified(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    qids = ops.ask_round(cfg, "T-1", [
        ("Use Postgres or SQLite?", "Postgres", None, "proceed"),
        ("Cut a v2 API or extend v1?", None, None, None),
    ])
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qids[0], "text": "Postgres", "accepted_recommendation": True})
    inbox.append_command(cfg.home, "T-1", "ans", {"qid": qids[1], "text": "let me think"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert not _dispatcher_phase_changes(cfg, "T-1")


def test_round_falls_through_when_qid_answered_twice(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    qid = ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"qid": qid, "text": "ok"})
    inbox.append_command(cfg.home, "T-1", "ans", {"qid": qid, "text": "yes"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert not _dispatcher_phase_changes(cfg, "T-1")


# --- AC5: every new negative shape still falls through ---------------------

def test_literal_answer_falls_through_for_non_proceed_recommendation(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    qid = _ask_with_recommend(cfg, "T-1", "Keep going?", "Hold off and gather more information",
                              "other")
    inbox.append_command(cfg.home, "T-1", "ans", {"qid": qid, "text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value


def test_echo_falls_through_for_non_proceed_recommendation(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    qid = _ask_with_recommend(cfg, "T-1", "Keep going?", "Research this further before deciding",
                              "other")
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qid, "text": "Research this further before deciding"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value


def test_marker_accept_falls_through_when_kind_undeclared(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    qid = _ask_with_recommend(cfg, "T-1", "Keep going?", "Yes, proceed", None)
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qid, "text": "Yes, proceed", "accepted_recommendation": True})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value


def test_reject_word_recommendation_falls_through_even_with_proceed_kind(cfg):
    """T-72's shape (a rejection recommendation) must fall through even when
    the asker mistakenly declared it "proceed" -- the reject-word defense in
    depth is unconditional."""
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    qid = _ask_with_recommend(cfg, "T-1", "Keep going?",
                              "Reject/close T-1 as not a real defect", "proceed")
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qid, "text": "Reject/close T-1 as not a real defect",
                          "accepted_recommendation": True})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value


@pytest.mark.parametrize("answer_text", [
    "yes but use sqlite",
    "ok, first check the CI config",
    "yes?",
])
def test_prefix_rule_rejects_qualified_or_questioning_answers(cfg, answer_text):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": answer_text})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value


def test_prefix_rule_rejects_answers_over_80_chars(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Proceed?")
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "yes " + "x" * 80})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value


def test_split_qid_falls_through(cfg):
    cfg.answer_fast_path = "on"
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Split the diff?", qid="split-T-1-abcd1234")
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"text": "ok", "qid": "split-T-1-abcd1234"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value


# --- AC6: shadow records the matched rule per qid; off is unaffected -------

def test_shadow_records_matched_rule_for_marker_accept(cfg):
    cfg.answer_fast_path = "shadow"
    _create(cfg, "T-1")
    qid = _ask_with_recommend(cfg, "T-1", "Proceed?", "Yes, proceed", "proceed")
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qid, "text": "Yes, proceed", "accepted_recommendation": True})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    decided = _fast_path_decided(cfg, "T-1")
    assert decided[-1]["payload"]["outcome"] == "would_route_answer"
    assert decided[-1]["payload"]["rules"] == {qid: "marker"}


def test_shadow_records_matched_rule_for_round(cfg):
    cfg.answer_fast_path = "shadow"
    _create(cfg, "T-1")
    qids = ops.ask_round(cfg, "T-1", [
        ("Use Postgres or SQLite?", "Postgres", None, "proceed"),
        ("Cut a v2 API or extend v1?", None, None, None),
    ])
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qids[0], "text": "Postgres", "accepted_recommendation": True})
    inbox.append_command(cfg.home, "T-1", "ans", {"qid": qids[1], "text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    decided = _fast_path_decided(cfg, "T-1")
    assert decided[-1]["payload"]["rules"] == {qids[0]: "marker", qids[1]: "literal"}


def test_off_round_shape_falls_through_with_no_fast_path_event(cfg):
    _create(cfg, "T-1")
    qids = ops.ask_round(cfg, "T-1", [
        ("Use Postgres or SQLite?", "Postgres", None, "proceed"),
        ("Cut a v2 API or extend v1?", None, None, None),
    ])
    inbox.append_command(cfg.home, "T-1", "ans",
                         {"qid": qids[0], "text": "Postgres", "accepted_recommendation": True})
    inbox.append_command(cfg.home, "T-1", "ans", {"qid": qids[1], "text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert not _fast_path_decided(cfg, "T-1")
