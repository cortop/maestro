"""Tests for the 'researching' phase, RESEARCH_PROPOSED event, and snapshot fields."""
import pytest

from maestro import event_log, snapshot as snap_mod
from maestro.statemachine import Phase, ACTIVE_PHASES, can_transition


# ── statemachine ──────────────────────────────────────────────────────────────

def test_ready_to_researching_allowed():
    assert can_transition(Phase.READY, Phase.RESEARCHING)


def test_researching_to_awaiting_human_allowed():
    assert can_transition(Phase.RESEARCHING, Phase.AWAITING_HUMAN)


def test_researching_to_degraded_allowed():
    assert can_transition(Phase.RESEARCHING, Phase.DEGRADED)


def test_researching_to_terminating_allowed():
    assert can_transition(Phase.RESEARCHING, Phase.TERMINATING)


def test_researching_to_done_allowed():
    assert can_transition(Phase.RESEARCHING, Phase.DONE)


def test_awaiting_human_to_researching_allowed():
    assert can_transition(Phase.AWAITING_HUMAN, Phase.RESEARCHING)


def test_researching_to_implementing_disallowed():
    assert not can_transition(Phase.RESEARCHING, Phase.IMPLEMENTING)


def test_researching_to_ready_disallowed():
    assert not can_transition(Phase.RESEARCHING, Phase.READY)


def test_done_to_researching_disallowed():
    assert not can_transition(Phase.DONE, Phase.RESEARCHING)


def test_researching_is_active_phase():
    assert Phase.RESEARCHING in ACTIVE_PHASES


# ── snapshot fold ─────────────────────────────────────────────────────────────

def test_ticket_created_kind_research(home):
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "x", "spec_hash": "abc", "kind": "research"}, actor="d")
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.kind == "research"


def test_ticket_created_kind_defaults_to_implementation(home):
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "x", "spec_hash": "abc"}, actor="d")
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.kind == "implementation"


def test_research_proposed_sets_proposal_path(home):
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "x", "spec_hash": "abc", "kind": "research"}, actor="d")
    event_log.append(home, "T-1", "ResearchProposed",
                     {"proposal_path": "tickets/T-1/proposal.md", "alternatives": ["a", "b"]},
                     actor="r")
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.proposal_path == "tickets/T-1/proposal.md"


def test_proposal_path_none_by_default(home):
    event_log.append(home, "T-1", "TicketCreated", {"title": "x"}, actor="d")
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.proposal_path is None


def test_snapshot_roundtrip_with_research_fields(home):
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "x", "kind": "research"}, actor="d")
    event_log.append(home, "T-1", "ResearchProposed",
                     {"proposal_path": "tickets/T-1/proposal.md", "alternatives": []},
                     actor="r")
    snap = snap_mod.rebuild(home, "T-1")
    loaded = snap_mod.load(home, "T-1")
    assert loaded.kind == "research"
    assert loaded.proposal_path == "tickets/T-1/proposal.md"
    assert loaded.to_dict() == snap.to_dict()
