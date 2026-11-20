"""T-302: reviews appear on the Reviews and Now tabs only, never on a ticket surface of the TUI.

Real `MaestroTUI` driven over a temp home with synthetic fixtures.
"""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Static
from textual.widgets._footer import FooterKey

from conftest import seed_review_ticket, seed_ticket
from maestro.tui import DecisionsScreen, ReviewScreen
from tui_support import _filter_idx, _make_app


def _run(home, body):
    async def _inner():
        app = _make_app(home)
        app._nudge_enabled = False
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause(0.2)
            await body(app, pilot)
            assert app._exception is None
    asyncio.run(_inner())


def _keys(table) -> list[str]:
    return [str(k.value) for k in table.rows]


def test_tickets_tab_excludes_review_but_now_keeps_it(home):
    seed_ticket(home, "T-1", "A ticket")
    seed_review_ticket(home)

    async def body(app, pilot):
        bar = str(app.query_one("#filter-bar", Static).render())
        app._filter_idx = _filter_idx("all")
        app._populate()
        assert _keys(app.query_one("#tickets", DataTable)) == ["T-1"]
        bar = str(app.query_one("#filter-bar", Static).render())
        assert "all(1)" in bar and "needs-you(0)" in bar
        groups = str(app.query_one("#now-groups", Static).render())
        assert "NEEDS YOU (1)" in groups and "review" in groups
    _run(home, body)


def test_own_pr_screen_omits_review_in_awaiting_ci(home):
    seed_ticket(home, "T-1", "A ticket", phase="awaiting-ci", pr=5)
    seed_review_ticket(home, phase="awaiting-ci")

    async def body(app, pilot):
        await pilot.press("R")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ReviewScreen)
        assert _keys(app.screen.query_one("#review-table", DataTable)) == ["T-1"]
        assert "Own PRs" in {k.description for k in app.query(FooterKey)} | {
            b.description for b in app.BINDINGS if getattr(b, "key", None) == "R"}
    _run(home, body)


def test_decisions_queue_has_no_review_question(home):
    seed_ticket(home, "T-1", "A ticket", phase="awaiting-human",
                questions={"q1": "Which way?"})
    seed_review_ticket(home, phase="awaiting-human")

    async def body(app, pilot):
        await pilot.press("W")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DecisionsScreen)
        assert {r["key"] for r in app.screen._rows} == {"T-1"}
    _run(home, body)


def test_palette_review_key_selects_it_on_reviews_tab(home):
    seed_ticket(home, "T-1", "A ticket")
    seed_review_ticket(home)

    async def body(app, pilot):
        assert any(k == "T-60" for k, _t, _p in app._palette_tickets)
        assert app._jump_to("T-60") is True
        await pilot.pause(0.2)
        assert app.query_one("TabbedContent").active == "tab-reviews"
        assert app._reviews_key == "T-60"
    _run(home, body)
