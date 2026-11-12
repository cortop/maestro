"""Ticket view-model: status sentence, per-phase rail and dispatch card."""
from __future__ import annotations

from conftest import seed_phase, seed_ticket
from maestro import event_log, snapshot as snap_mod, store
from maestro.statemachine import Phase
from maestro.view import ticket as tv


def _move(home, key, phase):
    event_log.append(home, key, "PhaseChanged", {"phase": phase, "reason": ""}, actor="r")


def test_rail_dwell_visits_current_and_failed(home):
    seed_ticket(home, "T-1", "rail", phase="ready")
    for p in ("implementing", "qa", "implementing", "qa"):
        _move(home, "T-1", p)
    snap_mod.rebuild(home, "T-1")
    now = store.now_epoch() + 100
    view = tv.ticket(home, "T-1", now)
    rail = {e.phase: e for e in view.rail}
    assert [e.phase for e in view.rail][:3] == ["triaging", "researching", "awaiting-human"]
    assert rail["implementing"].visits == 2
    assert rail["qa"].visits == 2 and rail["qa"].current and not rail["implementing"].current
    assert rail["qa"].failed          # first qa visit bounced back to implementing
    assert not rail["triaging"].failed and rail["researching"].visits == 0
    assert rail["qa"].dwell_s >= 100
    assert all(e.dwell_s == 0 for e in view.rail if e.visits == 0)


def test_status_sentence_by_phase(home):
    seed_ticket(home, "T-2", "s", phase="awaiting-ci", pr=5)
    snap = snap_mod.load(home, "T-2")
    assert tv.status_sentence(home, "T-2", snap) == "The dispatcher is waiting on CI."
    _move(home, "T-2", "in-review")
    snap = snap_mod.rebuild(home, "T-2")
    assert "you" in tv.status_sentence(home, "T-2", snap)
    seed_ticket(home, "T-3", "q", phase="awaiting-human", questions={"a": "ok?"})
    snap = snap_mod.load(home, "T-3")
    assert tv.status_sentence(home, "T-3", snap) == "Waiting on you to answer."
    seed_phase(home, "T-4", Phase.IMPLEMENTING)
    snap = snap_mod.load(home, "T-4")
    assert tv.status_sentence(home, "T-4", snap).startswith("An agent is working")


def test_ticket_view_carries_status_and_rail(home):
    seed_phase(home, "T-5", Phase.QA)
    view = tv.ticket(home, "T-5", store.now_epoch())
    assert view.status and view.rail


def test_dispatch_card(home, cfg):
    seed_phase(home, "T-6", Phase.READY)
    snap = snap_mod.load(home, "T-6")
    card = tv.dispatch_card(home, "T-6", snap, cfg, store.now_epoch())
    assert card["verdict"].startswith(("DUE", "NOT DUE"))
    assert card["claim"] == "no claim"
    assert card["attempts"].startswith("spawn attempts at seq")
    assert card["throttle"] and card["open_deps"] == []
