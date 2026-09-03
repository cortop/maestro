"""T-104: push maestro phase transitions to Linear status.

The only mock anywhere in this file is `FakeLinearTransport` -- the external
Linear HTTP boundary. Everything else (the phase-change gates, the exhaustive
phase->status mapping, the idempotent push, the soft-degrade on failure) is
exercised via the real `ops.set_phase`/`ops.finalize` entry points -- the ONLY
two places a `PhaseChanged`/`Finalized` event is ever appended, and so the
only two places T-104's push can ever fire from.
"""
import pytest

from maestro import event_log, ops, providers, snapshot as snap_mod, store
from maestro.dispatcher import spec_hash_on_disk
from maestro.providers.linear import LinearTracker, STATUS_BY_PHASE, _assert_exhaustive
from maestro.statemachine import Phase

ALL_STATES = [
    {"id": "s-todo", "name": "To do"},
    {"id": "s-prog", "name": "In Progress"},
    {"id": "s-review", "name": "In Review"},
    {"id": "s-done", "name": "Done"},
]


class FakeLinearTransport:
    """The only mock: the external Linear HTTP boundary."""

    def __init__(self, issues=(), *, states=None, issue_ids=None, fail_update=False):
        self.issues = {i["identifier"]: i for i in issues}
        self.states = states or {}          # identifier -> [{"id", "name"}, ...]
        self.issue_ids = issue_ids or {}     # identifier -> uuid
        self.fail_update = fail_update
        self.calls = []

    def search_issues(self, filter):
        self.calls.append(("search_issues", filter))
        return list(self.issues.values())

    def get_issue(self, identifier):
        self.calls.append(("get_issue", identifier))
        return self.issues.get(identifier, {})

    def get_comments(self, identifier):
        return []

    def get_workflow_states(self, identifier):
        self.calls.append(("get_workflow_states", identifier))
        return {
            "issue_id": self.issue_ids.get(identifier),
            "states": self.states.get(identifier, []),
        }

    def update_issue_state(self, issue_id, state_id):
        self.calls.append(("update_issue_state", issue_id, state_id))
        return not self.fail_update


def _seed_ticket(cfg, key, *, external_source=None, external_id=None):
    store.atomic_write(
        store.spec_path(cfg.home, key),
        f"# {key}: test\n\n## Acceptance criteria\n- [ ] does the thing\n",
    )
    payload = {"title": key, "spec_hash": spec_hash_on_disk(cfg.home, key)}
    if external_source:
        payload["external_source"] = external_source
    if external_id:
        payload["external_id"] = external_id
    event_log.append(cfg.home, key, "TicketCreated", payload, actor="test")
    snap_mod.rebuild(cfg.home, key)


def _seed_linear_ticket(cfg, monkeypatch, identifier, *, states=ALL_STATES, fail_update=False):
    """A Linear-linked ticket (a real AC, so the AWAITING_CI/QA gates can be
    satisfied) plus a fake tracker wired in via `providers.get_trackers`,
    exactly like `test_multi_tracker.py`'s convention."""
    key = f"LINEAR-{identifier}"
    _seed_ticket(cfg, key, external_source="linear", external_id=identifier)
    transport = FakeLinearTransport(
        states={identifier: states}, issue_ids={identifier: f"uuid-{identifier}"},
        fail_update=fail_update)
    tracker = LinearTracker({}, transport=transport)
    monkeypatch.setattr(providers, "get_trackers", lambda c: {"linear": tracker})
    cfg.awaiting_ci_qa_gate = False  # not what this file is testing; see test_ac_gates.py etc.
    return key, transport, tracker


# --- STATUS_BY_PHASE: the exhaustive mapping (AC5) -----------------------------

def test_status_by_phase_is_exhaustive_over_phase():
    assert set(STATUS_BY_PHASE) == set(Phase)


def test_status_by_phase_matches_the_spec_mapping():
    assert STATUS_BY_PHASE[Phase.READY] == "To do"
    assert STATUS_BY_PHASE[Phase.IMPLEMENTING] == "In Progress"
    assert STATUS_BY_PHASE[Phase.AWAITING_CI] == "In Review"
    assert STATUS_BY_PHASE[Phase.IN_REVIEW] == "In Review"
    assert STATUS_BY_PHASE[Phase.DONE] == "Done"
    for p in (Phase.TRIAGING, Phase.AWAITING_HUMAN, Phase.VERIFYING, Phase.QA,
              Phase.RESEARCHING, Phase.DEGRADED, Phase.TERMINATING):
        assert STATUS_BY_PHASE[p] is None


def test_new_phase_without_a_mapping_row_fails_the_build():
    """RB-9: a `Phase` member with no row in the table is a hard, import-time
    failure, not a silently-skipped push -- exercises the real
    `_assert_exhaustive` the module runs at import time, against a throwaway
    copy of the real table missing one entry."""
    incomplete = dict(STATUS_BY_PHASE)
    del incomplete[Phase.DONE]
    with pytest.raises(AssertionError, match="done"):
        _assert_exhaustive(Phase, incomplete)


def test_a_complete_mapping_does_not_raise():
    _assert_exhaustive(Phase, STATUS_BY_PHASE)  # no raise -- this IS the real table


# --- LinearTracker.transition: the real mutation (AC1) -------------------------

def test_transition_resolves_state_by_name_and_mutates():
    transport = FakeLinearTransport(
        states={"ENG-1": ALL_STATES}, issue_ids={"ENG-1": "uuid-1"})
    tracker = LinearTracker({}, transport=transport)

    tracker.transition("ENG-1", "In Progress")

    assert ("update_issue_state", "uuid-1", "s-prog") in transport.calls


def test_transition_raises_on_unknown_state_name():
    transport = FakeLinearTransport(
        states={"ENG-1": [{"id": "s-todo", "name": "To do"}]}, issue_ids={"ENG-1": "uuid-1"})
    tracker = LinearTracker({}, transport=transport)

    with pytest.raises(store.MaestroError, match="no workflow state"):
        tracker.transition("ENG-1", "Bogus")

    assert not any(c[0] == "update_issue_state" for c in transport.calls)


def test_transition_raises_when_issue_not_found():
    transport = FakeLinearTransport()  # no states/issue_ids registered for ENG-9
    tracker = LinearTracker({}, transport=transport)

    with pytest.raises(store.MaestroError, match="not found"):
        tracker.transition("ENG-9", "To do")


# --- The real phase walk (AC2) --------------------------------------------------

def test_walk_ready_to_done_produces_exactly_the_mapped_pushes_in_order(cfg, monkeypatch):
    key, transport, _ = _seed_linear_ticket(cfg, monkeypatch, "ENG-10")

    ops.set_phase(cfg, key, Phase.READY, actor="r")
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, actor="r")
    ops.set_phase(cfg, key, Phase.AWAITING_CI, actor="r", force=True)
    ops.finalize(cfg, key, actor="r")

    pushed = [e["payload"]["status"] for e in event_log.read(cfg.home, key)
              if e["type"] == "LinearStatusPushed"]
    assert pushed == ["To do", "In Progress", "In Review", "Done"]

    update_calls = [c for c in transport.calls if c[0] == "update_issue_state"]
    assert [c[2] for c in update_calls] == ["s-todo", "s-prog", "s-review", "s-done"]


# --- Idempotency (AC3) -----------------------------------------------------------

def test_rerunning_the_same_phase_pushes_nothing(cfg, monkeypatch):
    key, transport, _ = _seed_linear_ticket(cfg, monkeypatch, "ENG-11")

    ops.set_phase(cfg, key, Phase.READY, actor="r")
    assert len([c for c in transport.calls if c[0] == "update_issue_state"]) == 1

    # A second, genuinely new PhaseChanged event for the SAME phase (bounce
    # away and back, so this isn't just a step-id-deduped crash-replay).
    ops.set_phase(cfg, key, Phase.TRIAGING, actor="r")
    ops.set_phase(cfg, key, Phase.READY, actor="r")

    assert len([c for c in transport.calls if c[0] == "update_issue_state"]) == 1


def test_a_different_phase_at_the_same_target_status_pushes_nothing(cfg, monkeypatch):
    key, transport, _ = _seed_linear_ticket(cfg, monkeypatch, "ENG-12")

    ops.set_phase(cfg, key, Phase.READY, actor="r")
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, actor="r")
    ops.set_phase(cfg, key, Phase.AWAITING_CI, actor="r", force=True)
    updates_before = len([c for c in transport.calls if c[0] == "update_issue_state"])

    # awaiting-ci and in-review both map to "In Review" -- already at target.
    ops.set_phase(cfg, key, Phase.IN_REVIEW, actor="r", force=True)

    updates_after = len([c for c in transport.calls if c[0] == "update_issue_state"])
    assert updates_after == updates_before
    pushed = [e["payload"]["status"] for e in event_log.read(cfg.home, key)
              if e["type"] == "LinearStatusPushed"]
    assert pushed.count("In Review") == 1


# --- No identifier / soft-degrade (AC4) ------------------------------------------

def test_no_linear_identifier_makes_zero_linear_calls(cfg, monkeypatch):
    key = "T-1"
    _seed_ticket(cfg, key)  # no external_source/external_id at all

    transport = FakeLinearTransport(states={}, issue_ids={})
    tracker = LinearTracker({}, transport=transport)
    monkeypatch.setattr(providers, "get_trackers", lambda c: {"linear": tracker})
    cfg.awaiting_ci_qa_gate = False

    ops.set_phase(cfg, key, Phase.READY, actor="r")
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, actor="r")
    ops.set_phase(cfg, key, Phase.AWAITING_CI, actor="r", force=True)
    ops.finalize(cfg, key, actor="r")

    assert transport.calls == []
    assert not any(e["type"] == "LinearStatusPushed" for e in event_log.read(cfg.home, key))


def test_failed_push_degrades_soft_instead_of_wedging_the_reconcile(cfg, monkeypatch):
    # No states registered for this identifier -- `transition` will raise
    # "no workflow state named ..." when the push is attempted.
    key, transport, _ = _seed_linear_ticket(cfg, monkeypatch, "ENG-13", states=[])

    ev = ops.set_phase(cfg, key, Phase.READY, actor="r")

    assert ev is not None  # the phase change itself succeeded -- never wedged
    assert snap_mod.load(cfg.home, key).phase == Phase.READY.value
    events = event_log.read(cfg.home, key)
    assert not any(e["type"] == "LinearStatusPushed" for e in events)
    notes = [e for e in events if e["type"] == "Note" and "Linear status push" in e["payload"]["text"]]
    assert len(notes) == 1
