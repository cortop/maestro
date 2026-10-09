"""Mounted-app tests for InboxScreen and the inbox message modal."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input

from maestro import inbox, store
from maestro.tui import InboxScreen, _InboxModal
from tui_support import _filter_idx, _make_app, _run_modal_test


def test_inbox_screen_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-3", "view_inbox", InboxScreen)


def test_inbox_screen_shows_processed_and_pending_entries(seeded_home):
    """AC1/AC2: every inbox entry lists -- both consumed and unconsumed -- with
    the pending/processed split landing exactly at the cursor, same as
    ``inbox.pending``'s own split, proved via the real mounted app."""
    inbox.append_command(seeded_home, "T-3", "msg", {"text": "already handled"})
    inbox.append_command(seeded_home, "T-3", "msg", {"text": "second processed one"})
    inbox.append_command(seeded_home, "T-3", "msg", {"text": "still pending"})
    inbox.ack(seeded_home, "T-3")  # cursor -> 2: first two processed, third pending
    inbox.append_command(seeded_home, "T-3", "retry", {})  # appended after ack -> pending

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("view_inbox")
            await pilot.pause()
            screen = app.screen_stack[-1]
            assert isinstance(screen, InboxScreen)
            from textual.widgets import RichLog
            log_widget = screen.query_one("#inbox-full", RichLog)
            content = "\n".join(strip.text for strip in log_widget.lines)
            assert "already handled" in content
            assert "second processed one" in content
            assert "still pending" in content
            assert "PROCESSED" in content
            assert "PENDING" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_inbox_screen_empty_inbox_shows_placeholder_not_crash(seeded_home):
    """AC3: a ticket with no inbox file at all -- no inbox/<KEY>.jsonl ever
    written -- shows a placeholder instead of crashing."""
    assert not store.inbox_path(seeded_home, "T-5").exists()

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("view_inbox")
            await pilot.pause()
            screen = app.screen_stack[-1]
            assert isinstance(screen, InboxScreen)
            from textual.widgets import RichLog
            log_widget = screen.query_one("#inbox-full", RichLog)
            content = "\n".join(strip.text for strip in log_widget.lines)
            assert "empty" in content.lower()
            assert app._exception is None

    asyncio.run(_inner())


def test_inbox_action_notifies_when_no_ticket_selected(seeded_home):
    """AC3: the no-selection case notifies instead of pushing a screen or crashing."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = None
            notifications_before = len(app._notifications)
            await app.run_action("view_inbox")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert len(app._notifications) > notifications_before
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# (e) TUI-18: inbox-compose action works at any phase                          #
# --------------------------------------------------------------------------- #

def test_inbox_modal_open_and_escape(seeded_home):
    """'i' key opens _InboxModal on a selected ticket; escape dismisses it."""
    _run_modal_test(seeded_home, "T-3", "inbox_message", _InboxModal)


def test_inbox_message_writes_to_inbox(seeded_home):
    """Submitting _InboxModal appends a 'msg' command to the ticket inbox."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("inbox_message")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _InboxModal), "inbox modal did not open"
            modal = app.screen_stack[-1]
            inp = modal.query_one("#inbox-input", Input)
            inp.value = "hello from TUI-18 test"
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == 1, "modal should have dismissed after submit"
            assert app._exception is None

        # Verify the inbox entry was written
        from maestro import inbox
        entries = inbox.pending(seeded_home, "T-3")
        assert entries, "no entry written to T-3 inbox"
        last = entries[-1]
        assert last["command"] == "msg"
        assert last["args"]["text"] == "hello from TUI-18 test"

    asyncio.run(_inner())


def test_inbox_action_works_for_any_phase(seeded_home):
    """inbox_message action is accessible regardless of the ticket's current phase."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            for r in range(table.row_count):
                table.move_cursor(row=r)
                await pilot.pause()
                await app.run_action("inbox_message")
                await pilot.pause()
                assert isinstance(app.screen_stack[-1], _InboxModal), (
                    f"row {r}: expected _InboxModal, got {type(app.screen_stack[-1]).__name__}"
                )
                await pilot.press("escape")
                await pilot.pause()
                assert len(app.screen_stack) == 1, f"row {r}: modal did not dismiss"
                assert app._exception is None

    asyncio.run(_inner())
