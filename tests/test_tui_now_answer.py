"""Inline answering on the Now tab: right pane, enter-to-accept, degraded retry/discard (T-247)."""
from __future__ import annotations

import asyncio

from textual.widgets import Input, Static

from conftest import seed_ticket
from maestro import event_log, inbox
from maestro.tui import _AnswerModal, _ConfirmModal
from tui_support import _make_app

REC = "Approve: proceed with the plan"


def _run(home, body):
    async def _inner():
        app = _make_app(home)
        app._nudge_enabled = False  # a live nudge would spawn a real reconciler
        async with app.run_test(size=(140, 40)) as pilot:
            await _settle(pilot)
            await body(app, pilot)
            assert app._exception is None
    asyncio.run(_inner())


async def _settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def _detail(app) -> str:
    return str(app.query_one("#now-detail", Static).render())


def _groups(app) -> str:
    return str(app.query_one("#now-groups", Static).render())


def _seed_question(home, key="Q-1", rec=True):
    text = f"Pick it up?\n   Recommended: {REC}" if rec else "Pick it up?"
    seed_ticket(home, key, "needs an answer", phase="awaiting-human", questions={"q1": text})


def test_selecting_row_shows_question_recommendation_and_history(home):
    _seed_question(home)

    async def body(app, pilot):
        detail = _detail(app)
        assert "Q-1" in detail and "Pick it up?" in detail
        assert REC in detail and "Accept recommendation" in detail
        assert "Write an answer" in detail
        assert "How it got here" in detail
    _run(home, body)


def test_enter_accepts_recommendation_with_marker(home):
    _seed_question(home)

    async def body(app, pilot):
        await pilot.press("enter")
        await _settle(pilot)
        [cmd] = inbox.pending(home, "Q-1")
        assert cmd["command"] == "ans"
        assert cmd["args"] == {"text": REC, "qid": "q1", "accepted_recommendation": True}
        # the answered row leaves the needs-you group on the refresh
        assert "NEEDS YOU (0)" in _groups(app)
    _run(home, body)


def test_a_opens_answer_modal_and_typed_answer_has_no_marker(home):
    _seed_question(home)

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause()
        modal = app.screen_stack[-1]
        assert isinstance(modal, _AnswerModal)
        modal.dismiss("my own words")
        await _settle(pilot)
        [cmd] = inbox.pending(home, "Q-1")
        assert cmd["args"] == {"text": "my own words", "qid": "q1"}
    _run(home, body)


def test_enter_without_recommendation_opens_modal(home):
    _seed_question(home, rec=False)

    async def body(app, pilot):
        assert "No recommendation" in _detail(app)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen_stack[-1], _AnswerModal)
        assert inbox.pending(home, "Q-1") == []
    _run(home, body)


def test_down_moves_selection_between_rows(home):
    _seed_question(home, "Q-1")
    _seed_question(home, "Q-2")

    async def body(app, pilot):
        first = "Q-1" if "Q-1" in _detail(app) else "Q-2"
        await pilot.press("down")
        await pilot.pause()
        assert first not in _detail(app).split("\n")[0]
    _run(home, body)


def _seed_degraded(home):
    seed_ticket(home, "D-1", "broken", phase="implementing")
    event_log.append(home, "D-1", "PhaseChanged", {"phase": "degraded", "reason": "boom: disk on fire"},
                     actor="r")
    from maestro import snapshot as snap_mod
    snap_mod.rebuild(home, "D-1")


def test_degraded_row_offers_retry_and_confirmed_discard(home):
    _seed_degraded(home)

    async def body(app, pilot):
        detail = _detail(app)
        assert "retry" in detail and "discard" in detail
        await pilot.press("ctrl+d")
        await pilot.pause()
        modal = app.screen_stack[-1]
        assert isinstance(modal, _ConfirmModal)
        assert inbox.pending(home, "D-1") == []  # nothing queued before confirming
        modal.query_one("#confirm-input", Input).value = "D-1"
        await pilot.pause()
        modal.query_one("#confirm-ok").press()
        await _settle(pilot)
        assert [c["command"] for c in inbox.pending(home, "D-1")] == ["discard"]
        assert "NEEDS YOU (0)" in _groups(app)
    _run(home, body)


def test_degraded_retry_queues_command(home):
    _seed_degraded(home)

    async def body(app, pilot):
        await pilot.press("ctrl+r")
        await _settle(pilot)
        assert [c["command"] for c in inbox.pending(home, "D-1")] == ["retry"]
    _run(home, body)
