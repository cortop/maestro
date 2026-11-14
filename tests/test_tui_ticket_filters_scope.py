"""The ticket filter controls render only inside the Tickets tab (T-258)."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input, TabbedContent

from conftest import seed_ticket
from tui_support import _filter_idx, _make_app

_IDS = ("#filter-bar", "#query-bar")


def _visible(app, wid) -> bool:
    w = app.query_one(wid)
    return bool(w.display and w.region.height > 0 and w.visible)


def _keys(app):
    t = app.query_one("#tickets", DataTable)
    return sorted(str(t.get_row_at(r)[0]) for r in range(t.row_count))


def test_filters_hidden_off_tickets_and_below_tab_bar(home):
    seed_ticket(home, "S-1", "one", phase="ready")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.query_one("#tabs", TabbedContent).active == "tab-now"
            assert not _visible(app, "#filter-bar")
            await pilot.press("3")
            await pilot.pause()
            assert not _visible(app, "#filter-bar")
            await pilot.press("2")
            await pilot.pause()
            bar = app.query_one("#filter-bar")
            assert _visible(app, "#filter-bar")
            assert bar.ancestors_with_self and app.query_one("#tab-tickets") in bar.ancestors
            tabs = app.query_one("Tabs")
            assert bar.region.y >= tabs.region.y + tabs.region.height
            assert app._exception is None

    asyncio.run(_inner())


def test_switching_via_keys_and_state_persists(home):
    seed_ticket(home, "S-1", "one", phase="ready")
    seed_ticket(home, "S-2", "two", phase="qa")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            for tab, shown in (("1", False), ("2", True), ("3", False), ("2", True)):
                await pilot.press(tab)
                await pilot.pause()
                assert _visible(app, "#filter-bar") is shown
                assert app._exception is None
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.press("slash")
            await pilot.press(*"phase:ready")
            await pilot.pause(0.4)
            assert _keys(app) == ["S-1"]
            await pilot.press("enter")
            await pilot.press("1")
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            assert _keys(app) == ["S-1"]
            assert app.query_one("#query-bar", Input).value == "phase:ready"
            assert app._exception is None

    asyncio.run(_inner())


def test_filter_keys_noop_off_tickets_work_on_tickets(home):
    seed_ticket(home, "S-1", "one", phase="ready")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            idx = app._filter_idx
            for tab in ("1", "3"):
                await pilot.press(tab)
                await pilot.pause()
                await pilot.press("f")
                await pilot.press("slash")
                await pilot.pause()
                assert app._filter_idx == idx
                assert not app.query_one("#query-bar", Input).display
                assert app._exception is None
            await pilot.press("2")
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()
            assert app._filter_idx != idx
            await pilot.press("slash")
            await pilot.pause()
            assert app.query_one("#query-bar", Input).display
            assert app._exception is None

    asyncio.run(_inner())
