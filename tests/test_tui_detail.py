"""Mounted-app tests for DetailScreen, the detail pane and EventsScreen."""
from __future__ import annotations

import asyncio
import json

from textual.widgets import DataTable, Static

from conftest import seed_ticket
from maestro import event_log, snapshot as snap_mod, store
from maestro.tui import DetailScreen, EventsScreen, _EventPayloadModal
from tui_support import _filter_idx, _make_app, _run_modal_test


def test_detail_screen_open_and_escape(seeded_home):
    """Enter on a selected row opens DetailScreen (fullscreen right panel); Escape closes it."""
    _run_modal_test(seeded_home, "T-3", "focus_detail", DetailScreen)


def test_enter_key_on_focused_table_opens_detail(seeded_home):
    """Pressing the real Enter key on the focused DataTable opens DetailScreen.

    A focused DataTable consumes Enter (emitting RowSelected), so the app-level
    `enter` binding never fires — only on_data_table_row_selected reaches the
    detail view. run_action('focus_detail') would mask this, so press the key.
    """
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            before = len(app.screen_stack)
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == before + 1, "Enter did not open a screen"
            assert isinstance(app.screen_stack[-1], DetailScreen)
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == before
            assert app._exception is None

    asyncio.run(_inner())


def test_detail_screen_shows_detail_and_events(seeded_home):
    """DetailScreen mounts, populates #ds-detail and #ds-events without error."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("focus_detail")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            screen = app.screen_stack[-1]
            screen.query_one("#ds-detail", Static)  # widget must exist
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_detail_screen_shows_stacked_pr_entries(home):
    """T-127 AC5: a 3-entry stacked ticket's DetailScreen lists every PR in the
    stack, in order, with its own state -- proven by mounting the real app and
    pressing real keys, not by mocking query_one/push_screen."""
    seed_ticket(home, "T-1", "stacked PR ticket", phase="qa")
    for i in range(3):
        event_log.append(home, "T-1", "PrOpened", {
            "number": 200 + i, "url": f"https://example.com/pull/{200 + i}", "draft": True,
            "stack": {"index": i, "total": 3, "branch": f"maestro/T-1-{i+1}",
                      "base": "main" if i == 0 else f"maestro/T-1-{i}"},
        }, actor="r")
    snap_mod.rebuild(home, "T-1")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            assert app._selected_key == "T-1"

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            ds_text = app.screen_stack[-1].query_one("#ds-detail", Static).render().plain
            assert "#200" in ds_text and "#201" in ds_text and "#202" in ds_text
            assert "1/3" in ds_text and "2/3" in ds_text and "3/3" in ds_text
            await pilot.press("escape")
            await pilot.pause()

            assert app._exception is None

    asyncio.run(_inner())


def test_detail_pane_and_screen_render_title_from_spec_at_row_zero(home):
    """AD-7 replaces GA-18's tier-rendering test (the Tier line is gone --
    there is no more tier to render): proves both the compact #detail pane and
    the full DetailScreen (#ds-detail) still mount clean and show the ticket's
    title for the selected row."""
    seed_ticket(home, "T-1", "auto-approved change", phase="ready")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()

            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            assert app._selected_key == "T-1"
            assert "auto-approved change" in app.query_one("#detail", Static).render().plain

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            ds_text = app.screen_stack[-1].query_one("#ds-detail", Static).render().plain
            assert "auto-approved change" in ds_text
            await pilot.press("escape")
            await pilot.pause()

            assert app._exception is None

    asyncio.run(_inner())


def test_detail_pane_surfaces_provider_marker_for_a_provider_caused_degrade(home):
    """T-89 (AC5): a degraded ticket whose Failed/Stalled payload carries
    kind="provider" reads as provider-caused in the detail pane rather than a
    bare watchdog timeout -- proven by mounting the real app."""
    seed_ticket(home, "T-1", "outage casualty", phase="implementing")
    event_log.append(home, "T-1", "Failed",
                     {"error": "watchdog: no output for over 600s (pid 123)",
                      "kind": "provider", "state": "no_network"}, actor="dispatcher")
    event_log.append(home, "T-1", "Stalled",
                     {"reason": "1 failures: watchdog: no output for over 600s (pid 123)",
                      "kind": "provider", "state": "no_network"}, actor="dispatcher")
    snap_mod.rebuild(home, "T-1")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            assert app._selected_key == "T-1"
            content = app.query_one("#detail", Static).render().plain
            assert "PROVIDER" in content
            assert "no_network" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_events_screen_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-3", "view_events", EventsScreen)


def test_detail_pane_and_screen_render_title_from_spec(home):
    """A ticket whose log carries no TicketCreated folds to `title = None`, but
    its spec's H1 has the title -- both the compact #detail pane and the full
    DetailScreen (#ds-detail) must show it (`snapshot.display_title`), and the
    normally-minted case must keep rendering the folded title."""
    # no TicketCreated: exactly what a directory-discovered / already-advanced key looks like
    store.atomic_write(store.spec_path(home, "X-1"),
                       "# X-1: title only in the spec\n\napproval_tier: 1\n\n## Intent\nb\n")
    event_log.append(home, "X-1", "SpecObserved", {"spec_hash": "abc"}, actor="r")
    event_log.append(home, "X-1", "PhaseChanged", {"phase": "ready", "reason": ""}, actor="r")
    snap_mod.rebuild(home, "X-1")
    seed_ticket(home, "X-2", "folded title wins", phase="ready")

    def _title_line(static: Static) -> str:
        return static.render().plain.splitlines()[0]

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()

            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            assert app._selected_key == "X-1"
            assert "title only in the spec" in _title_line(app.query_one("#detail", Static))

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            assert "title only in the spec" in _title_line(
                app.screen_stack[-1].query_one("#ds-detail", Static))
            await pilot.press("escape")
            await pilot.pause()

            # the folded title still wins for a normally-minted ticket
            table.move_cursor(row=1)
            await pilot.pause()
            assert app._selected_key == "X-2"
            assert "folded title wins" in _title_line(app.query_one("#detail", Static))

            # and the board row itself carries it
            titles = {str(table.get_row_at(r)[0]): str(table.get_row_at(r)[2])
                      for r in range(table.row_count)}
            assert "title only in the spec" in titles["X-1"]

            assert app._exception is None

    asyncio.run(_inner())


def test_compact_and_project_rebuild_do_not_race_on_shared_derived_files(seeded_home):
    """RB-5: `ops.compact` (`action_compact`) and the projection rebuild
    (`action_project_rebuild`) both run on Textual worker *threads* inside
    this one process and both write under `derived/*` -- the exact shape the
    bug was in (`store.atomic_write`'s old pid-only temp name collided across
    threads of one process, surfacing as a bare `OSError`/`FileNotFoundError`
    escaping into a Textual callback). Confirming the compact modal and then
    immediately triggering the rebuild -- before waiting for either worker --
    lets Textual actually run both concurrently; `app._exception is None`
    proves neither raced the other into a torn/missing temp file."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"

            await app.run_action("compact")  # opens the y/n confirm modal
            await pilot.pause()
            await pilot.press("y")  # confirm -> spawns the compact worker thread
            await app.run_action("project_rebuild")  # spawns the rebuild worker thread
            await pilot.pause()

            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())

    # Both writers actually landed -- not just "didn't crash".
    assert store.events_archive_path(seeded_home, "T-3").exists()
    dashboards = list((seeded_home / "derived").glob("*.md"))
    assert dashboards, "projection rebuild did not write any dashboard"


# --------------------------------------------------------------------------- #
# T-172: live DetailScreen, event table, payload modal                        #
# --------------------------------------------------------------------------- #

def _ds_cursor_seq(table):
    return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value


def test_detail_screen_live_refresh_keeps_cursor(seeded_home, monkeypatch):
    monkeypatch.setattr(DetailScreen, "REFRESH_INTERVAL", 0.2)
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#ds-events", DataTable)
            before_rows = table.row_count
            table.move_cursor(row=1)
            await pilot.pause()
            seq = _ds_cursor_seq(table)
            event_log.append(seeded_home, "T-3", "PhaseChanged",
                             {"phase": "qa", "reason": "x"}, actor="r")
            await pilot.pause(0.8)
            assert table.row_count == before_rows + 1
            assert _ds_cursor_seq(table) == seq
            assert "qa" in str(screen.query_one("#ds-dwell", Static).render())
            assert app._exception is None
    asyncio.run(_inner())


def test_detail_screen_refresh_gated_on_log_signature(seeded_home, monkeypatch):
    monkeypatch.setattr(DetailScreen, "REFRESH_INTERVAL", 0.2)
    async def _inner():
        for _ in range(30):
            event_log.append(seeded_home, "T-3", "Checked", {"n": 1}, actor="r")
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 20)) as pilot:
            await pilot.pause()
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            screen = app.screen
            screen._tail_mode = False
            screen.action_refresh()
            table = screen.query_one("#ds-events", DataTable)
            await pilot.pause()
            table.move_cursor(row=table.row_count - 1)
            await pilot.pause()
            table.move_cursor(row=3)
            await pilot.pause()
            calls = []
            orig = screen._refresh
            screen._refresh = lambda: (calls.append(1), orig())
            state = (table.cursor_row, table.scroll_y)
            await pilot.pause(0.7)
            assert not calls, "refresh must not re-render without a log change"
            assert (table.cursor_row, table.scroll_y) == state
            assert app._exception is None
    asyncio.run(_inner())


def test_detail_event_row_opens_payload_modal(seeded_home):
    async def _inner():
        excerpt = "boom [red]x[/red] " + "y" * 600
        event_log.append(seeded_home, "T-3", "CiObserved",
                         {"state": "failure", "failure_excerpt": excerpt}, actor="d")
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            table = app.screen.query_one("#ds-events", DataTable)
            table.focus()
            table.move_cursor(row=table.row_count - 1)
            await pilot.pause()
            depth = len(app.screen_stack)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, _EventPayloadModal)
            assert len(app.screen_stack) == depth + 1
            assert excerpt in str(app.screen.query_one("#payload-json", Static).render())
            await pilot.press("c")
            await pilot.pause()
            assert json.loads(app.clipboard)["payload"]["failure_excerpt"] == excerpt
            assert app._selected_key == "T-3"
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen)
            assert sum(isinstance(sc, DetailScreen) for sc in app.screen_stack) == 1
            assert app._exception is None
    asyncio.run(_inner())


def test_events_screen_table_and_payload_modal(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, EventsScreen)
            assert app.screen._key == "T-3"
            table = app.screen.query_one("#events-full", DataTable)
            assert table.row_count == len(event_log.read(seeded_home, "T-3"))
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, _EventPayloadModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, EventsScreen)
            assert app._exception is None
    asyncio.run(_inner())
