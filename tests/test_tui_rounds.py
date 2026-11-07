"""Mounted-app tests for RoundsScreen (`d`, T-228)."""
from __future__ import annotations

import asyncio
import time

from textual.widgets import DataTable, RichLog, Static

from conftest import seed_phase
from maestro import event_log, snapshot as snap_mod
from maestro.statemachine import Phase
from maestro.tui import LogsScreen, RoundsScreen
from maestro.tui.modals import _TextViewModal
from tui_support import _make_app


def _seed(home, key="T-1"):
    seed_phase(home, key, Phase.IMPLEMENTING)
    event_log.append(home, key, "ReviewFeedbackReceived",
                     {"comment_id": "c1", "author": "alice", "state": "COMMENTED",
                      "body": "please rename the helper", "path": "x.py", "line": 3},
                     actor="t")
    snap_mod.rebuild(home, key)
    log_dir = home / "agent-logs" / key
    log_dir.mkdir(parents=True, exist_ok=True)
    epoch = time.time() + 5
    path = log_dir / f"reconcile-{key}-{epoch:.6f}.log"
    path.write_text("synthetic session line\n")
    return path


def _run(home, body, key="T-1"):
    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = key
            await body(app, pilot)
            assert app._exception is None
    asyncio.run(_inner())


def test_d_opens_rounds_with_header_and_rows(home):
    _seed(home)

    async def body(app, pilot):
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, RoundsScreen)
        header = str(app.screen.query_one("#rounds-header", Static).render())
        assert "T-1" in header and "Rounds" in header
        assert app.screen.query_one("#rounds-table", DataTable).row_count >= 1
    _run(home, body)


def test_enter_opens_logs_on_first_session_then_r_opens_threads(home):
    path = _seed(home)

    async def body(app, pilot):
        await pilot.press("d")
        await pilot.pause()
        table = app.screen.query_one("#rounds-table", DataTable)
        rows = app.screen._rounds.rows
        table.move_cursor(row=next(i for i, r in enumerate(rows) if r.first_session))
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LogsScreen)
        assert app.screen._pick["path"] == str(path)
        await pilot.pause(0.5)
        log = app.screen.query_one("#logs-view", RichLog)
        assert any("synthetic session line" in str(line) for line in log.lines)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, RoundsScreen)
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, _TextViewModal)
    _run(home, body)
