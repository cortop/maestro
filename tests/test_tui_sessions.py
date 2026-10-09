"""Mounted-app tests for SessionsScreen (`B`): Burners + Claims tabs (T-169)."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable

from conftest import _result_record, _write_stream_log
from maestro import claims, event_log, store
from maestro.tui import LogsScreen, SessionsScreen, _ConfirmModal
from tui_support import _make_app


async def _sessions_table(app, pilot, table_id, want_rows=1):
    for _ in range(100):
        await pilot.pause(0.1)
        table = app.screen.query_one(table_id, DataTable)
        if table.row_count >= want_rows:
            return table
    raise AssertionError(f"{table_id} never reached {want_rows} row(s)")


def _row_cells(table, key):
    return [str(c) for c in table.get_row(key)]


def test_sessions_screen_lists_per_key_spend(seeded_home):
    _write_stream_log(seeded_home, "T-3", store.now_epoch() - 60, [_result_record(0.50)])

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("B")
            await pilot.pause()
            assert isinstance(app.screen, SessionsScreen)
            table = await _sessions_table(app, pilot, "#burners-table")
            assert "$0.50" in _row_cells(table, "T-3")
            assert app._exception is None

    asyncio.run(_inner())


# T-170: FleetScreen doctor checks table + remedies                           #


def test_sessions_screen_flags_burning_key(seeded_home):
    for _ in range(5):
        event_log.append(seeded_home, "T-3", "Failed", {"error": "boom"}, actor="r")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("B")
            table = await _sessions_table(app, pilot, "#burners-table")
            assert "repeated failure" in _row_cells(table, "T-3")
            assert app._exception is None

    asyncio.run(_inner())


def test_sessions_burner_enter_jumps_board_cursor(seeded_home):
    _write_stream_log(seeded_home, "T-3", store.now_epoch() - 60, [_result_record(0.50)])

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("B")
            await _sessions_table(app, pilot, "#burners-table")
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(app.screen, SessionsScreen)
            assert app._selected_key == "T-3"
            assert app._exception is None

    asyncio.run(_inner())


def _claim_setup(home, child_process):
    claims.write_claim(home, "T-3", child_process.pid, "reconcile-T-3")
    claims.write_claim(home, "T-5", 2_000_000_000, "reconcile-T-5")


def test_sessions_claims_tab_shows_verdicts(seeded_home, child_process):
    _claim_setup(seeded_home, child_process)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("B")
            await pilot.pause()
            await pilot.press("2")
            table = await _sessions_table(app, pilot, "#claims-table", 2)
            t3, t5 = _row_cells(table, "T-3"), _row_cells(table, "T-5")
            assert t3[3] == "confirmed" and t3[4] == "yes"
            assert t5[4] == "no"
            table.move_cursor(row=table.get_row_index("T-3"))
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, LogsScreen) and app.screen._key == "T-3"
            assert app._exception is None

    asyncio.run(_inner())


def test_sessions_purge_releases_only_stale_claims(seeded_home, child_process):
    _claim_setup(seeded_home, child_process)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("B")
            await pilot.pause()
            await pilot.press("2")
            await _sessions_table(app, pilot, "#claims-table", 2)
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, _ConfirmModal)
            await pilot.press("enter")  # default is Cancel
            await pilot.pause()
            assert claims.claim_path(seeded_home, "T-3").exists()
            assert claims.claim_path(seeded_home, "T-5").exists()
            await pilot.press("x")
            await pilot.pause()
            await pilot.press("y")
            for _ in range(50):
                await pilot.pause(0.1)
                if not claims.claim_path(seeded_home, "T-5").exists():
                    break
            assert not claims.claim_path(seeded_home, "T-5").exists()
            assert claims.claim_path(seeded_home, "T-3").exists()
            assert child_process.poll() is None
            assert app._exception is None

    asyncio.run(_inner())
