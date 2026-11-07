"""Mounted-app tests for RoundsScreen session expansion and the LogsScreen picker labels (T-229)."""
from __future__ import annotations

import asyncio
import json
import time

from textual.widgets import DataTable, Static

from conftest import seed_phase
from maestro import event_log, snapshot as snap_mod
from maestro.statemachine import Phase
from maestro.tui import LogsScreen, RoundsScreen
from maestro.tui.modals import _SessionPickModal
from tui_support import _make_app

KEY = "T-1"


def _seed(home):
    seed_phase(home, KEY, Phase.IMPLEMENTING)
    snap_mod.rebuild(home, KEY)
    log_dir = home / "agent-logs" / KEY
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"reconcile-{KEY}-{time.time() + 5:.6f}.stream.jsonl"
    recs = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Step: Editing the module"},
            {"type": "tool_use", "id": "1", "name": "Edit", "input": {"file_path": "/x/a.py"}}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "Synthetic final words."},
    ]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


def _run(home, body):
    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = KEY
            await body(app, pilot)
            assert app._exception is None
    asyncio.run(_inner())


def test_enter_expands_round_then_opens_session_log(home):
    path = _seed(home)

    async def body(app, pilot):
        await pilot.press("d")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RoundsScreen)
        table = screen.query_one("#rounds-table", DataTable)
        table.move_cursor(row=next(i for i, r in enumerate(screen._rounds.rows) if r.sessions))
        n = table.row_count
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert table.row_count == n + 1  # one session row under the round
        detail = str(screen.query_one("#rounds-detail", Static).render())
        assert "Editing the module" in detail and "Synthetic final words." in detail
        assert "Change 1" in detail
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LogsScreen)
        assert app.screen._pick["path"] == str(path)
    _run(home, body)


def test_logs_picker_labels_are_digest_one_liners(home):
    _seed(home)

    async def body(app, pilot):
        await pilot.press("d")
        await pilot.pause()
        app.push_screen(LogsScreen(home, KEY))
        await pilot.pause(0.5)
        app.screen.action_pick_session()
        await pilot.pause(0.5)
        assert isinstance(app.screen, _SessionPickModal)
        assert "1 calls" in app.screen._labels[0] and "0/1/0/0" in app.screen._labels[0]
    _run(home, body)
