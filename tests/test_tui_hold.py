"""Mounted-app tests for per-ticket hold (`h`, T-175)."""
from __future__ import annotations

from textual.widgets import DataTable

from maestro import store
from maestro.tui import _ConfirmModal, HoldModal
from tui_support import _filter_idx, _make_app


# --------------------------------------------------------------------------- #
# T-175: per-ticket hold via `h`                                              #
# --------------------------------------------------------------------------- #

def test_hold_key_roundtrip(seeded_home):
    from maestro import fleet

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], HoldModal)
            await pilot.press("3", "0", "m", "enter")
            await pilot.pause()
            await pilot.press(*"pairing", "enter")
            await pilot.pause()
            st = fleet.hold_state(seeded_home, "T-3", store.now_epoch())
            assert st is not None and st["reason"] == "pairing" and st["until"]
            app._filter_idx = _filter_idx("held")
            app._populate()
            await pilot.pause()
            table = app.query_one(DataTable)
            assert [str(rk.value) for rk in table.rows] == ["T-3"]
            assert "⏸" in str(table.get_row("T-3")[0])
            app._selected_key = "T-3"
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _ConfirmModal)
            await pilot.press("enter")
            await pilot.pause()
            assert fleet.hold_state(seeded_home, "T-3", store.now_epoch()) is not None
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            assert fleet.hold_state(seeded_home, "T-3", store.now_epoch()) is None
            assert app._exception is None
