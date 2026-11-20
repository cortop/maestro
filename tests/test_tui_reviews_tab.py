"""T-300: the TUI's Reviews tab (key 4) -- list, brief, finding edits, mark reviewed, empty board.

Real `MaestroTUI` driven with real keys over a temp home; only the code-host boundary (`FakeVCS`)
is faked. Fixtures are synthetic.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from textual.widgets import Static, TabbedContent

from conftest import seed_review_ticket, seed_ticket
from maestro import fleet, snapshot as snap_mod
from maestro.tui import _ConfirmModal
from tui_support import _make_app
from test_tui_review_brief import FakeVCS, HEAD, KEY


def _run(home, body, vcs=None):
    async def _inner():
        app = _make_app(home)
        app._nudge_enabled = False
        app._vcs = vcs
        async with app.run_test(size=(150, 50)) as pilot:
            await _settle(pilot)
            await body(app, pilot)
            assert app._exception is None
    asyncio.run(_inner())


async def _settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause(0.2)


def _text(app, wid) -> str:
    return str(app.query_one(wid, Static).render())


def _ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _seed(home, **kw):
    kw.setdefault("requester", "Alice Martin")
    kw.setdefault("requested_ts", _ago(12))
    kw.setdefault("title", "Fix login timeout")
    return seed_review_ticket(home, **kw)


def test_key_4_lists_a_brief_ready_review(home):
    _seed(home)

    async def body(app, pilot):
        await pilot.press("4")
        await _settle(pilot)
        assert app.query_one("#tabs", TabbedContent).active == "tab-reviews"
        lst = _text(app, "#reviews-list")
        for want in ("NEEDS YOU (1)", "#7", "Fix login timeout", "acme/widgets", "< Alice Martin", "12m"):
            assert want in lst, lst
        det = _text(app, "#reviews-detail")
        assert "asked by Alice Martin" in det and "request-changes" in det
        assert "Adds a <b>parser</b>." in det and "src/a.py:3" in det
    _run(home, body)


def test_toggle_edit_move_and_create_on_the_reviews_tab(home):
    _seed(home)
    vcs = FakeVCS()

    async def body(app, pilot):
        await pilot.press("4")
        await _settle(pilot)
        await pilot.press("right", "space")  # exclude the nit
        await pilot.pause()
        assert "1 of 2 included" in _text(app, "#reviews-detail")
        await pilot.press("left", "u")
        await pilot.pause()
        app.screen_stack[-1].dismiss("Guard it, then retest.")
        await pilot.pause()
        assert "(edited)" in _text(app, "#reviews-detail")
        await pilot.press("C")
        await pilot.pause()
        assert isinstance(app.screen_stack[-1], _ConfirmModal)
        await pilot.press("y")
        await _settle(pilot)
        assert vcs.created == [{"head_sha": HEAD, "body": "", "comments": [
            {"path": "src/a.py", "line": 3, "body": "Guard it, then retest."}]}]
        assert snap_mod.load(home, KEY).review_pending["review_id"] == "9001"
    _run(home, body, vcs)


def test_mark_reviewed_moves_the_row_out_of_the_open_groups(home):
    _seed(home)

    async def body(app, pilot):
        await pilot.press("4")
        await _settle(pilot)
        await pilot.press("X")
        await pilot.pause()
        assert isinstance(app.screen_stack[-1], _ConfirmModal)
        await pilot.press("y")
        await _settle(pilot)
        assert snap_mod.load(home, KEY).phase == "done"
        lst = _text(app, "#reviews-list")
        assert "NEEDS YOU (0)" in lst and "RECENT (1)" in lst
    _run(home, body)


def test_hold_on_the_reviews_tab_never_acts_on_the_now_ticket(home):
    seed_ticket(home, "T-10", "needs an answer", phase="awaiting-human", questions={"q1": "Proceed?"})
    _seed(home)

    async def body(app, pilot):
        await pilot.press("down")  # a different Now card than the review (or the same list, other row)
        await pilot.pause()
        now_key = app._target_key()
        await pilot.press("4")
        await _settle(pilot)
        assert app._target_key() == KEY
        assert app.check_action("hold", ()) is False
        await pilot.press("h")
        await pilot.pause()
        assert not fleet.hold_path(home, "T-10").exists()
        assert not fleet.hold_path(home, KEY).exists()
        assert now_key in ("T-10", KEY)
    _run(home, body)


def test_empty_board_shows_an_empty_state_and_keys_raise_nothing(home):
    async def body(app, pilot):
        await pilot.press("4")
        await _settle(pilot)
        assert "No reviews yet" in _text(app, "#reviews-list")
        for key in ("4", "X", "Y", "U", "C", "space", "u", "left", "right", "up", "down", "O", "V"):
            await pilot.press(key)
            await pilot.pause()
            if len(app.screen_stack) > 1:
                await pilot.press("escape")
                await pilot.pause()
    _run(home, body)


def test_missing_requester_is_a_dash_and_markup_title_is_literal(home):
    seed_review_ticket(home, title="[bold red]Boom[/] fix", requester=None, requested_ts=None)

    async def body(app, pilot):
        await pilot.press("4")
        await _settle(pilot)
        lst = _text(app, "#reviews-list")
        assert "[bold red]Boom[/] fix" in lst and "< -" in lst
        assert "asked by -" in _text(app, "#reviews-detail")
    _run(home, body)


def test_number_keys_still_select_the_other_tabs(home):
    _seed(home)

    async def body(app, pilot):
        tabs = app.query_one("#tabs", TabbedContent)
        for key, tab in (("2", "tab-tickets"), ("3", "tab-fleet"), ("1", "tab-now"), ("4", "tab-reviews")):
            await pilot.press(key)
            await pilot.pause()
            assert tabs.active == tab
    _run(home, body)
