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
        "outcome": "would_route_answer", "reason": "would approve: ok"}
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
        "outcome": "answer_routed", "reason": "approved: ok"}


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
        "outcome": "answer_routed", "reason": "approved: ok"}
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
