"""Mounted-app tests for human input nudging a key-scoped sweep (T-153)."""
from __future__ import annotations

import asyncio

from textual.widgets import Input, Select, Static

from maestro import fleet as fleet_mod, inbox, store
from maestro.tui import _CreateModal, _InboxModal
from tui_support import _capture_toasts, _make_app, _settle


def test_tui_answer_nudges_key_scoped_sweep(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            toasts = _capture_toasts(app)
            app._selected_key = "T-1"
            await app.run_action("answer")
            await pilot.pause()
            for _ in range(2):  # two open questions
                await pilot.press("a", "n", "s")
                await pilot.press("ctrl+s")
                await pilot.pause()
            await _settle(app, pilot)
            assert [s[0] for s in app.dry.spawned] == ["T-1"]
            assert any("T-1: spawned" in t for t in toasts), toasts
            assert app._exception is None

    asyncio.run(_inner())


def test_tui_nudge_respects_config_off(seeded_home):
    (seeded_home / "config.toml").write_text(
        "[maestro]\nnudge_on_human_input = false\n", encoding="utf-8")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await app.run_action("answer")
            await pilot.pause()
            for _ in range(2):
                await pilot.press("a", "n", "s")
                await pilot.press("ctrl+s")
                await pilot.pause()
            await _settle(app, pilot)
            assert app.dry.spawned == []
            assert len(inbox.pending(seeded_home, "T-1")) == 2
            assert app._exception is None

    asyncio.run(_inner())


def test_tui_nudge_reports_fleet_paused(seeded_home):
    fleet_mod.pause(seeded_home, reason="test")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            toasts = _capture_toasts(app)
            app._selected_key = "T-2"
            await pilot.press("ctrl+r")
            await _settle(app, pilot)
            assert app.dry.spawned == []
            assert any("T-2" in t and "paused" in t for t in toasts), toasts
            assert app._exception is None

    asyncio.run(_inner())


def test_tui_create_nudge_mints_key(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            toasts = _capture_toasts(app)
            await app.run_action("create")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _CreateModal)
            modal.query_one("#create-title", Input).value = "Brand new thing"
            modal.query_one("#create-prefix", Select).value = "T"
            await pilot.pause()
            await pilot.press("ctrl+enter")
            await _settle(app, pilot)
            assert app._exception is None
        minted = [t for t in toasts if t.startswith("minted ")]
        assert minted and "T-6" in minted[0], toasts
        assert store.spec_path(seeded_home, "T-6").exists()

    asyncio.run(_inner())


def test_tui_nudge_toggle_off(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _settle(app, pilot)
            await pilot.press("N")
            await pilot.pause()
            assert "nudge:off" in str(app.query_one("#fleet-badge", Static).render())
            app._selected_key = "T-3"
            await app.run_action("inbox_message")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _InboxModal)
            await pilot.press("h", "i", "enter")
            await _settle(app, pilot)
            assert app.dry.spawned == []
            assert len(inbox.pending(seeded_home, "T-3")) == 1
            assert app._exception is None

    asyncio.run(_inner())
