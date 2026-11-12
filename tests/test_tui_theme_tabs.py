"""Neon Ledger theme and the Now / Tickets / Fleet tabs on the real MaestroTUI."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Static, TabbedContent

from conftest import seed_ticket
from tui_support import _make_app


def _run(home, body):
    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await body(app, pilot)
            assert app._exception is None
    asyncio.run(_inner())


def _seed(home):
    seed_ticket(home, "N-1", "needs a human", phase="awaiting-human")
    seed_ticket(home, "N-2", "building", phase="implementing")
    seed_ticket(home, "N-3", "waiting its turn", phase="ready")


async def _settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def test_theme_registered_and_default(home):
    async def body(app, pilot):
        assert app.theme == "neon-ledger"
        assert "neon-ledger" in app.available_themes
        theme = app.available_themes["neon-ledger"]
        assert theme.background.lower() == "#07070a"
        assert theme.primary.lower() == "#ebcb55"
        assert theme.secondary.lower() == "#00e5ff"
        assert theme.error.lower() == "#ff7fa6"
    _run(home, body)


def test_opens_on_now_with_groups_and_status(home):
    _seed(home)

    async def body(app, pilot):
        await _settle(pilot)
        assert app.query_one("#tabs", TabbedContent).active == "tab-now"
        groups = str(app.query_one("#now-groups", Static).render())
        for label in ("NEEDS YOU", "IN FLIGHT", "QUEUED", "N-1", "N-3"):
            assert label in groups
        status = str(app.query_one("#now-status", Static).render())
        assert "fleet" in status and "live session" in status and "$" in status
    _run(home, body)


def test_keys_switch_tabs_and_tickets_table_unchanged(home):
    _seed(home)

    async def body(app, pilot):
        tabs = app.query_one("#tabs", TabbedContent)
        await pilot.press("2")
        await pilot.pause()
        assert tabs.active == "tab-tickets"
        assert app.focused is app.query_one("#tickets", DataTable)
        assert app.query_one("#tickets", DataTable).row_count >= 1
        await pilot.press("3")
        await _settle(pilot)
        assert tabs.active == "tab-fleet"
        assert "Loading" not in str(app.query_one("#fleet-tab", Static).render())
        await pilot.press("1")
        await pilot.pause()
        assert tabs.active == "tab-now"
    _run(home, body)
