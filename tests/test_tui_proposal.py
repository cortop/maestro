"""Mounted-app tests for the ProposalScreen decision bar (T-159)."""
from __future__ import annotations

import asyncio

from textual.widgets import Static

from conftest import seed_ticket
from maestro import inbox, ops as ops_mod
from maestro.tui import DetailScreen, ProposalScreen, _ConfirmModal, _DirectionModal
from tui_support import _make_app


def test_proposal_screen_no_proposal_notifies(seeded_home):
    """action_view_proposal on DetailScreen notifies when no proposal.md exists."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("focus_detail")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            screen = app.screen_stack[-1]
            notifs_before = len(app._notifications)
            await screen.run_action("view_proposal")
            await pilot.pause()
            assert len(app._notifications) > notifs_before, "expected a notification when no proposal.md"
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_proposal_screen_opens_with_proposal(seeded_home):
    """action_view_proposal opens ProposalScreen when proposal.md exists."""
    proposal_path = seeded_home / "tickets" / "T-3" / "proposal.md"
    proposal_path.write_text("# Proposal\n\nThis is a test proposal.")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("focus_detail")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            screen = app.screen_stack[-1]
            await screen.run_action("view_proposal")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], ProposalScreen), (
                f"expected ProposalScreen, got {type(app.screen_stack[-1]).__name__}"
            )
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


# --- T-159: ProposalScreen decision bar --------------------------------------

_PROPOSAL_MD = "# Research\n\n## Recommended\nUse the event log\n\n## Alternative 1\nUse sqlite\n\n## Alternative 2\nUse files\n"


def _proposal_home(home, *, open_q=True, body=_PROPOSAL_MD):
    qs = {"research-approval-T-6": "Approve?" + ops_mod._RECOMMEND_SEP + "Approve the recommended approach"} if open_q else None
    seed_ticket(home, "T-6", "research", phase="awaiting-human", questions=qs)
    (home / "tickets" / "T-6" / "proposal.md").write_text(body)
    return home


async def _open_proposal(app, pilot):
    await pilot.pause()
    app.push_screen(ProposalScreen(app._home, "T-6"))
    await pilot.pause()
    return app.screen_stack[-1]


def _proposal_ans(home):
    return [c for c in inbox.pending(home, "T-6") if c["command"] == "ans"]


def test_proposal_bar_selects_alternative(home):
    _proposal_home(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_proposal(app, pilot)
            bar = str(screen.query_one("#proposal-bar", Static).render())
            assert screen.query_one("#proposal-bar").display
            for want in ("Recommended", "Use sqlite", "Use files"):
                assert want in bar
            await pilot.press("2")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _ConfirmModal)
            await pilot.press("y")
            await pilot.pause()
            assert [c["args"] for c in _proposal_ans(home)] == [{"qid": "research-approval-T-6", "text": "alternative 2"}]
            await pilot.press("2")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], ProposalScreen)
            assert len(_proposal_ans(home)) == 1
        assert app._exception is None

    asyncio.run(_inner())


def test_proposal_bar_alternative_needs_confirm(home):
    _proposal_home(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_proposal(app, pilot)
            await pilot.press("2")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], ProposalScreen)
            await pilot.press("7")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], ProposalScreen)
            assert _proposal_ans(home) == []
        assert app._exception is None

    asyncio.run(_inner())


def test_proposal_bar_accepts_recommendation(home):
    _proposal_home(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_proposal(app, pilot)
            await pilot.press("y")
            await pilot.pause()
            assert [c["args"] for c in _proposal_ans(home)] == [{
                "qid": "research-approval-T-6", "text": "Approve the recommended approach",
                "accepted_recommendation": True}]
        assert app._exception is None

    asyncio.run(_inner())


def test_proposal_bar_needs_more(home):
    _proposal_home(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_proposal(app, pilot)
            await pilot.press("m")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _DirectionModal)
            await pilot.press("escape")
            await pilot.pause()
            assert _proposal_ans(home) == []
            await pilot.press("m")
            await pilot.pause()
            await pilot.press(*"compare with sqlite", "enter")
            await pilot.pause()
            assert [c["args"] for c in _proposal_ans(home)] == [
                {"qid": "research-approval-T-6", "text": "needs more: compare with sqlite"}]
        assert app._exception is None

    asyncio.run(_inner())


def test_proposal_bar_hidden_without_approval_question(home):
    _proposal_home(home, open_q=False)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_proposal(app, pilot)
            assert not screen.query_one("#proposal-bar").display
            for action in ("accept_recommendation", "pick_alternative", "needs_more"):
                assert screen.check_action(action, ()) is False
            await pilot.press("y", "2", "m")  # disabled here; `m` falls through to the app's menu
            await pilot.pause()
            assert not any(isinstance(s, (_ConfirmModal, _DirectionModal)) for s in app.screen_stack)
            assert _proposal_ans(home) == []
        assert app._exception is None

    asyncio.run(_inner())


def test_proposal_section_jumps(home):
    body = "# Research\n\n" + "".join(f"## Section {i}\n" + "line\n\n" * 30 for i in range(4))
    _proposal_home(home, open_q=False, body=body)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 20)) as pilot:
            screen = await _open_proposal(app, pilot)
            scroll = screen.query_one("#proposal-body")
            assert scroll.scroll_y == 0
            await pilot.press("j")
            await pilot.pause()
            first = scroll.scroll_y
            assert first > 0
            await pilot.press("j")
            await pilot.pause()
            assert scroll.scroll_y > first
            await pilot.press("k")
            await pilot.pause()
            assert scroll.scroll_y == first
        assert app._exception is None

    asyncio.run(_inner())
