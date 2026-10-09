"""Mounted-app tests for the add-AC modal (`A`, T-112)."""
from __future__ import annotations

import asyncio

from textual.widgets import Input

from maestro import snapshot as snap_mod, store
from maestro.tui import _AddAcModal
from tui_support import _make_app, _run_modal_test


# --------------------------------------------------------------------------- #
# T-112: 'A' -> _AddAcModal -> ops.add_ac                                     #
# --------------------------------------------------------------------------- #

def test_add_ac_action_notifies_when_no_ticket_selected(seeded_home):
    """The `key is None` guard the binding sweep exercises for every key: pressing
    'A' with nothing selected notifies instead of pushing a screen."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = None
            notifications_before = len(app._notifications)
            await app.run_action("add_ac")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert len(app._notifications) > notifications_before
            assert app._exception is None

    asyncio.run(_inner())


def test_add_ac_modal_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-5", "add_ac", _AddAcModal)


def test_add_ac_modal_round_trip_appends_ac_to_spec(seeded_home):
    """AC2: typing text and confirming writes the new AC to spec.md via
    `ops.add_ac`, and notifies."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            notifications_before = len(app._notifications)
            await app.run_action("add_ac")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _AddAcModal)
            modal.query_one("#add-ac-input", Input).value = "a new thing works"
            await pilot.press("enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1
            assert len(app._notifications) > notifications_before

    asyncio.run(_inner())
    after = store.spec_path(seeded_home, "T-5").read_text()
    assert snap_mod.parse_acs(after) == ["ok", "a new thing works"]


def test_add_ac_modal_cancel_writes_nothing(seeded_home):
    """Cancelling (Esc) writes nothing -- spec bytes on disk are unchanged."""
    before_bytes = store.spec_path(seeded_home, "T-5").read_bytes()

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("add_ac")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _AddAcModal)
            modal.query_one("#add-ac-input", Input).value = "should never land"
            await pilot.press("escape")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())
    assert store.spec_path(seeded_home, "T-5").read_bytes() == before_bytes


def test_add_ac_modal_blank_text_dismisses_without_writing(seeded_home):
    """Submitting blank/whitespace-only text is treated the same as cancel --
    the modal's own `_submit` dismisses with `None` rather than calling
    `ops.add_ac` with nothing to add."""
    before_bytes = store.spec_path(seeded_home, "T-5").read_bytes()

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("add_ac")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _AddAcModal)
            await pilot.press("enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())
    assert store.spec_path(seeded_home, "T-5").read_bytes() == before_bytes
