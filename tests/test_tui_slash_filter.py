"""Mounted-app tests for the `/` live filter (T-166)."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input, Static

from conftest import seed_ticket
from maestro.tui import _FILTERS
from tui_support import _filter_idx, _make_app


# --------------------------------------------------------------------------- #
# T-166: `/` live filter                                                       #
# --------------------------------------------------------------------------- #

def _table_keys(app):
    table = app.query_one("#tickets", DataTable)
    return [str(table.get_row_at(r)[0]) for r in range(table.row_count)]


async def _type_query(pilot, text):
    await pilot.press("slash")
    await pilot.press(*text)
    await pilot.pause(0.4)


def test_slash_filter_phase_query(home):
    seed_ticket(home, "S-1", "one", phase="ready")
    seed_ticket(home, "S-2", "two", phase="ready")
    seed_ticket(home, "S-3", "three", phase="qa")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._populate()
            assert len(_table_keys(app)) == 3
            await _type_query(pilot, "phase:ready")
            assert sorted(_table_keys(app)) == ["S-1", "S-2"]
            bar = str(app.query_one("#filter-bar", Static).render())
            assert "/ phase:ready" in bar and "(2)" in bar
            assert app._exception is None

    asyncio.run(_inner())


def test_slash_filter_input_swallows_app_keys(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            idx = app._filter_idx
            await _type_query(pilot, "q")
            await pilot.press("f")
            await pilot.pause(0.4)
            assert app.is_running and app._filter_idx == idx
            assert app.query_one("#query-bar", Input).value == "qf"
            assert app._exception is None

    asyncio.run(_inner())


def test_slash_filter_enter_keeps_esc_clears(home):
    seed_ticket(home, "S-1", "one", phase="ready")
    seed_ticket(home, "S-3", "three", phase="qa")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._populate()
            await _type_query(pilot, "phase:qa")
            await pilot.press("enter")
            await pilot.pause()
            assert app.focused is app.query_one("#tickets", DataTable)
            await pilot.press("r")
            await pilot.pause(0.4)
            assert _table_keys(app) == ["S-3"]
            await pilot.press("escape")
            await pilot.pause()
            assert sorted(_table_keys(app)) == ["S-1", "S-3"]
            assert not app.query_one("#query-bar", Input).display
            assert "/ phase" not in str(app.query_one("#filter-bar", Static).render())
            assert app._exception is None

    asyncio.run(_inner())


def test_slash_filter_combines_with_preset(home):
    seed_ticket(home, "S-1", "asks", phase="awaiting-human", questions={"q1": "ok?"})
    seed_ticket(home, "S-2", "stuck", phase="degraded")
    seed_ticket(home, "S-3", "busy", phase="implementing")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert _FILTERS[app._filter_idx][0] == "needs-you"
            assert sorted(_table_keys(app)) == ["S-1", "S-2"]
            await _type_query(pilot, "!q:open")
            assert _table_keys(app) == ["S-2"]
            assert app._exception is None

    asyncio.run(_inner())


def test_slash_filter_empty_and_invalid(home):
    seed_ticket(home, "S-1", "one", phase="ready")
    seed_ticket(home, "S-2", "two", phase="qa")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._populate()
            await _type_query(pilot, "phase:qa")
            assert _table_keys(app) == ["S-2"]
            await pilot.press("x")  # "phase:qax" matches nothing
            await pilot.pause(0.4)
            assert _table_keys(app) == [] and app._selected_key is None
            await pilot.press("backspace", "backspace", "backspace", "backspace", "backspace",
                              "backspace", "backspace", "backspace", "backspace", "backspace")
            await pilot.press(*"bogus:1")
            await pilot.pause(0.4)
            assert app._query_error
            assert "bogus" in str(app.query_one("#filter-bar", Static).render())
            assert app._exception is None

    asyncio.run(_inner())


def test_slash_filter_board_only(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert not app._on_board()
            await pilot.press("slash")
            await pilot.pause()
            assert not app.query_one("#query-bar", Input).display
            assert app._exception is None

    asyncio.run(_inner())
