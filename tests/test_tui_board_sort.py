"""Mounted-app tests for sortable board columns and ticket-action targeting (T-161, T-155)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from textual.widgets import DataTable, Static

from conftest import seed_phase, seed_ticket
from maestro import event_log, inbox, dispatcher as disp_mod, snapshot as snap_mod, store
from maestro.projection import ticket_rows
from maestro.tui import DecisionsScreen, DetailScreen, FleetScreen, _FILTERS, _InboxModal
from tui_support import _filter_idx, _make_app, _spy_notify


# --------------------------------------------------------------------------- #
# T-161: sortable board columns + Idle column                                 #
# --------------------------------------------------------------------------- #

def _seed_sort_board(home):
    for i in (1, 2, 3, 4, 5):
        seed_phase(home, f"T-{i}", disp_mod.Phase.READY)
    for n in range(3):
        event_log.append(home, "T-3", "Failed", {"error": f"boom {n}"}, actor="r")
    snap_mod.rebuild(home, "T-3")


def _cursor_key(app):
    table = app.query_one("#tickets", DataTable)
    return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value


def _row_values(app):
    table = app.query_one("#tickets", DataTable)
    return [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]


def _bar(app):
    return str(app.query_one("#filter-bar").render())


def test_idle_column_shows_time_since_last_event(home, monkeypatch):
    real = store.iso_now
    old = time.time() - 2 * 3600 - 30
    monkeypatch.setattr(store, "iso_now",
                        lambda: datetime.fromtimestamp(old, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    seed_phase(home, "T-1", disp_mod.Phase.READY)
    monkeypatch.setattr(store, "iso_now", real)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            labels = [c.label.plain for c in table.columns.values()]
            assert labels[labels.index("Deps") + 1] == "Idle"
            assert table.get_row("T-1")[labels.index("Idle")] == "2h"
            assert app._exception is None

    asyncio.run(_inner())


def test_actions_target_visible_ticket(seeded_home):
    from textual.widgets import Input as _Input

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            app._selected_key = "T-3"
            seen = _spy_notify(app)
            app.push_screen(DetailScreen(seeded_home, "T-1"))
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _InboxModal)
            modal.query_one("#inbox-input", _Input).value = "for T-1"
            await pilot.press("enter")
            await pilot.pause()
            assert [c["args"]["text"] for c in inbox.pending(seeded_home, "T-1")] == ["for T-1"]
            assert inbox.pending(seeded_home, "T-3") == []
            assert any("T-1" in m for m, _ in seen)
            assert app._exception is None

    asyncio.run(_inner())


def test_sort_by_fails_survives_refresh(home):
    _seed_sort_board(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            for _ in range(6):  # Key, Phase, Title, PR, CI, Fails
                await pilot.press(">")
            await pilot.pause()
            assert _row_values(app)[0] == "T-3"
            assert "↓ Fails" in _bar(app)
            order = _row_values(app)
            key = _cursor_key(app)
            app._populate()
            await pilot.pause()
            assert _row_values(app) == order
            assert _cursor_key(app) == key
            assert app._exception is None

    asyncio.run(_inner())


def test_sort_direction_flip(home):
    _seed_sort_board(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            for _ in range(6):
                await pilot.press(">")
            await pilot.press("<")
            await pilot.pause()
            assert _row_values(app)[-1] == "T-3"
            assert "↑ Fails" in _bar(app)
            assert app._exception is None

    asyncio.run(_inner())


def test_header_click_sorts_and_reverses(home):
    _seed_sort_board(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            cols = list(table.columns.values())
            x = sum(c.get_render_width(table) for c in cols[:5]) + 2
            await pilot.click("#tickets", offset=(x, 0))
            await pilot.pause()
            assert _row_values(app)[0] == "T-3"
            assert "↓ Fails" in _bar(app)
            await pilot.click("#tickets", offset=(x, 0))
            await pilot.pause()
            assert _row_values(app)[-1] == "T-3"
            assert "↑ Fails" in _bar(app)
            assert app._exception is None

    asyncio.run(_inner())


def test_empty_filter_clears_selection(home):
    seed_ticket(home, "T-3", "implementing", phase="implementing", pr=15)
    seed_ticket(home, "T-5", "ready", phase="ready")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            app._selected_key = "T-3"
            seen = _spy_notify(app)
            await pilot.press("f")
            await pilot.pause()
            assert _FILTERS[app._filter_idx][0] == "needs-you"
            assert app._selected_key is None
            assert "No tickets match" in app.query_one("#detail", Static).render().plain
            before = len(seen)
            await pilot.press("c")
            await pilot.pause()
            assert any(sev == "warning" for _, sev in seen[before:])
            await pilot.press("a")  # T-164: no target -> the decision queue, not a warning
            await pilot.pause()
            assert isinstance(app.screen, DecisionsScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert inbox.pending(home, "T-3") == [] and inbox.pending(home, "T-5") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_sort_cycle_returns_to_default_order(home):
    _seed_sort_board(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            for _ in range(8):  # through Idle
                await pilot.press(">")
            await pilot.pause()
            assert "Idle" in _bar(app)
            await pilot.press(">")
            await pilot.pause()
            assert _row_values(app) == [r[-1] for r in ticket_rows(home)]
            assert "↓" not in _bar(app) and "↑" not in _bar(app)
            assert app._exception is None

    asyncio.run(_inner())


def test_sort_keeps_cursor_on_selected_key(home):
    _seed_sort_board(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.move_cursor(row=table.get_row_index("T-5"))
            await pilot.pause()
            assert app._selected_key == "T-5"
            for _ in range(6):
                await pilot.press(">")
            await pilot.pause()
            assert app._selected_key == "T-5"
            assert _cursor_key(app) == "T-5"
            assert table.cursor_row == table.get_row_index("T-5")
            assert app._exception is None

    asyncio.run(_inner())


def test_ticket_actions_hidden_off_ticket_screens(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("F")
            await pilot.pause()
            assert isinstance(app.screen, FleetScreen)
            actions = {b.binding.action for b in app.screen.active_bindings.values()}
            assert not actions & {"answer", "discard"}
            await pilot.press("escape")
            await pilot.pause()
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            actions = {b.binding.action for b in app.screen.active_bindings.values()}
            assert "cycle_filter" not in actions
            assert app._exception is None

    asyncio.run(_inner())


def test_retry_dimmed_unless_degraded(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            assert app.check_action("retry", ()) is None
            assert app.check_action("discard", ()) is None
            app._selected_key = "T-2"
            assert app.check_action("retry", ()) is True
            app._selected_key = "T-1"
            assert app.check_action("answer", ()) is True
            app._selected_key = "T-3"
            assert app.check_action("answer", ()) is True  # T-164: opens the queue

    asyncio.run(_inner())


def test_question_mark_opens_help(seeded_home):
    from textual.widgets import HelpPanel

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert app.screen.query(HelpPanel)
            await pilot.press("question_mark")
            await pilot.pause()
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert app.screen.query(HelpPanel)
            assert app._exception is None

    asyncio.run(_inner())
