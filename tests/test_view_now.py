"""`maestro.view.now` groups every open ticket once and carries each group's display fields."""
from __future__ import annotations

import os

from conftest import seed_phase, seed_ticket
from maestro import claims, dispatcher as disp, event_log, fleet as fleet_mod, snapshot as snap_mod, store
from maestro.config import Config
from maestro.view import now as now_view
from maestro.view import why as why_view


def _seed_dependent(home, key, dep):
    store.atomic_write(store.spec_path(home, key), f"---\ndependsOn: [{dep}]\n---\n# {key}\n\n"
                       "## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": "ready"}, actor="r")
    snap_mod.rebuild(home, key)


def _board(home):
    seed_ticket(home, "T-1", "asks", phase="awaiting-human",
                questions={"q1": "1/2. Which way?\n   Recommended: left"})
    seed_ticket(home, "T-2", "broken", phase="degraded")
    seed_ticket(home, "T-3", "coding", phase="implementing")
    seed_ticket(home, "T-4", "ci", phase="awaiting-ci")
    seed_phase(home, "T-5")
    _seed_dependent(home, "T-6", "T-1")
    seed_ticket(home, "T-7", "done", phase="done")
    claims.write_claim(home, "T-3", os.getpid(), "test")


def test_every_open_ticket_lands_in_exactly_one_group(home):
    _board(home)
    view = now_view.build(home, Config(home=home), store.now_epoch())
    groups = {
        "needs": {c.key for c in view.needs_you},
        "flight": {r.key for r in view.in_flight},
        "queued": {r.key for r in view.queued},
    }
    assert groups["needs"] == {"T-1", "T-2"}
    assert groups["flight"] == {"T-3", "T-4"}
    assert groups["queued"] == {"T-5", "T-6"}
    assert "T-7" not in set().union(*groups.values())


def test_cards_carry_question_dependents_and_failure_reason(home):
    _board(home)
    event_log.append(home, "T-2", "Failed", {"error": "boom"}, actor="d")
    snap_mod.rebuild(home, "T-2")
    view = now_view.build(home, Config(home=home), store.now_epoch() + 90)
    q = next(c for c in view.needs_you if c.key == "T-1")
    assert (q.qid, q.body, q.recommendation) == ("q1", "Which way?", "left")
    assert q.waiting_s >= 90 and q.dependents == ("T-6",)
    d = next(c for c in view.needs_you if c.key == "T-2")
    assert isinstance(d, now_view.DegradedCard) and "boom" in d.reason


def test_in_flight_rows_name_owner_and_round(home):
    _board(home)
    rows = {r.key: r for r in now_view.build(home, Config(home=home)).in_flight}
    assert rows["T-3"].owner == "agent" and rows["T-3"].round_label.startswith("Implementing")
    assert rows["T-4"].owner == "dispatcher"


def test_queued_reason_matches_now_verdict_and_hold(home):
    _board(home)
    now = store.now_epoch()
    view = now_view.build(home, Config(home=home), now)
    rows = {r.key: r for r in view.queued}
    expected = why_view.now_verdict(home, "T-6", snap_mod.load(home, "T-6"), now)[1]
    assert rows["T-6"].reason == expected and "T-1" in rows["T-6"].reason
    fleet_mod.hold(home, "T-5", until=now + 3600, reason="wait")
    held = {r.key: r for r in now_view.build(home, Config(home=home), now).queued}
    assert held["T-5"].reason.startswith("held until ")
