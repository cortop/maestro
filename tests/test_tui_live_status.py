"""T-226: the real TUI shows Doing on the board row and why / doing / now in the detail pane."""
from __future__ import annotations

import asyncio
import json

from textual.widgets import DataTable, Static

from maestro import event_log, snapshot as snap_mod
from tui_support import _claim, _filter_idx, _make_app


def test_row_doing_and_detail_why_doing_now(seeded_home, tmp_path):
    event_log.append(seeded_home, "T-3", "PhaseChanged",
                     {"phase": "implementing", "reason": "worktree ready"}, actor="r")
    snap_mod.rebuild(seeded_home, "T-3")
    log = _claim(seeded_home, "T-3", tmp_path)
    rec = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Step: Wiring [the] board"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make t"}}]}}
    log.write_text(json.dumps(rec) + "\n")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._kick_live()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            cells = [str(c) for c in table.get_row("T-3")]
            assert "Wiring [the] board" in cells
            app._selected_key = "T-3"
            app._show_detail("T-3", app._snaps["T-3"])
            await pilot.pause()
            text = str(app.query_one("#detail", Static).content)
            assert "Why" in text and "Implementing" in text
            assert "Wiring" in text and "make t" in text
            assert app._exception is None

    asyncio.run(_inner())
