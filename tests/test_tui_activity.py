"""Mounted-app tests for ActivityScreen, the board-wide ticker (T-173)."""
from __future__ import annotations

import asyncio

import pytest
from textual.widgets import DataTable

from maestro import event_log
from maestro.tui import ActivityScreen, DetailScreen
from tui_support import _make_app, _wait_for


# --------------------------------------------------------------------------- #
# T-173: ActivityScreen (board-wide ticker)                                   #
# --------------------------------------------------------------------------- #

async def _open_activity(app, pilot):
    ActivityScreen.POLL_INTERVAL = 0.2
    await pilot.press("T")
    for _ in range(40):
        await pilot.pause(0.05)
        if isinstance(app.screen, ActivityScreen) and app.screen._ready:
            break
    await pilot.pause(0.2)
    assert isinstance(app.screen, ActivityScreen)
    return app.screen


@pytest.fixture(autouse=False)
def _fast_activity(monkeypatch):
    monkeypatch.setattr(ActivityScreen, "POLL_INTERVAL", 0.2)


def _row_keys(screen):
    return [k.value for k in screen.query_one("#act-table", DataTable).rows]


def test_activity_screen_tails_new_event_to_top(seeded_home, _fast_activity):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            screen = await _open_activity(app, pilot)
            assert _row_keys(screen)
            ev = event_log.append(seeded_home, "T-3", "Note", {"text": "hello [x]"}, actor="t")
            top = f"T-3:{ev['seq']}"
            assert await _wait_for(pilot, lambda: _row_keys(screen)[:1] == [top])
            assert _row_keys(screen).count(top) == 1
            assert app._exception is None

    asyncio.run(_inner())


def test_activity_hot_keys_pause_and_categories(seeded_home, _fast_activity):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            screen = await _open_activity(app, pilot)
            for i in range(40):
                event_log.append(seeded_home, "T-3", "RequeueScheduled", {"at": i}, actor="t")
            assert await _wait_for(pilot, lambda: screen.hot_keys[:1] and screen.hot_keys[0][0] == "T-3"
                                   and screen.hot_keys[0][1] >= 40)
            await pilot.press("space")
            before = len(_row_keys(screen))
            ev = event_log.append(seeded_home, "T-4", "Note", {"text": "held"}, actor="t")
            held = f"T-4:{ev['seq']}"
            assert await _wait_for(pilot, lambda: screen._held)
            assert len(_row_keys(screen)) == before and held not in _row_keys(screen)
            await pilot.press("space")
            await pilot.pause()
            assert _row_keys(screen)[0] == held
            await pilot.press("6")  # RequeueScheduled lives in group 6
            await pilot.pause()
            assert not any(k for k in _row_keys(screen)
                           if screen.query_one("#act-table", DataTable).get_row(k)[2].plain == "RequeueScheduled")
            assert app._exception is None

    asyncio.run(_inner())


def _tree_sizes(home):
    return {str(p): p.stat().st_size for d in ("events", "derived/claims")
            if (home / d).is_dir() for p in (home / d).rglob("*") if p.is_file()}


def test_activity_enter_opens_detail_read_only(seeded_home, _fast_activity):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            screen = await _open_activity(app, pilot)
            table = screen.query_one("#act-table", DataTable)
            idx = next(i for i, k in enumerate(_row_keys(screen)) if k.startswith("T-3:"))
            table.move_cursor(row=idx)
            await pilot.pause()
            before = _tree_sizes(seeded_home)
            depth = len(app.screen_stack)
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == depth + 1
            assert isinstance(app.screen, DetailScreen) and app.screen._key == "T-3"
            assert app._selected_key == "T-1"
            await pilot.pause(0.5)
            assert _tree_sizes(seeded_home) == before
            assert app._exception is None

    asyncio.run(_inner())
