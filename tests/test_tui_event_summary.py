"""Mounted-app test: a failing AcQaVerdict shows its evidence in the events table."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable

from maestro import event_log
from maestro.tui import ActivityScreen
from tui_support import _make_app


def test_failing_verdict_evidence_visible_in_activity_table(seeded_home):
    event_log.append(seeded_home, "T-3", "AcQaVerdict",
                     {"ac_hash": "h", "ac_index": 1, "ac_text": "does a thing",
                      "verdict": "fail", "evidence": "synthetic gap [x]\nsecond line",
                      "axis": "correctness"}, actor="t")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("T")
            for _ in range(40):
                await pilot.pause(0.05)
                if isinstance(app.screen, ActivityScreen) and app.screen._ready:
                    break
            table = app.screen.query_one("#act-table", DataTable)
            cells = [str(c) for r in range(table.row_count) for c in table.get_row_at(r)]
            assert any("FAIL: synthetic gap [x]" in c for c in cells), cells
            assert not any("second line" in c for c in cells)
            assert app._exception is None

    asyncio.run(_inner())
