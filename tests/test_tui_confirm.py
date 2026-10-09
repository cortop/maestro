"""Mounted-app tests for default-No confirms, typed-key discard and live-claim release refusal (T-154)."""
from __future__ import annotations

import asyncio
import subprocess
import sys

from textual.widgets import Input

from maestro import claims, inbox, store
from maestro.tui import _CmdModal, _ConfirmModal
from tui_support import _make_app


# --------------------------------------------------------------------------- #
# T-154: default-No confirms, typed-key discard, release refuses live claims  #
# --------------------------------------------------------------------------- #

def test_confirm_modal_defaults_to_no(seeded_home):
    """Enter on the (Cancel-focused) confirm cancels; only `y` compacts."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("compact")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _ConfirmModal)
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not isinstance(app.screen_stack[-1], _ConfirmModal)
            assert not store.events_archive_path(seeded_home, "T-3").exists()

            await app.run_action("compact")
            await pilot.pause()
            await pilot.press("y")
            for _ in range(50):  # threaded worker: poll, don't race a fixed pause
                await pilot.pause(0.1)
                if store.events_archive_path(seeded_home, "T-3").exists():
                    break
            assert store.events_archive_path(seeded_home, "T-3").exists()
            assert app._exception is None

    asyncio.run(_inner())


def test_discard_requires_typed_key(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-2"
            await pilot.press("ctrl+d")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ConfirmModal)
            await pilot.press("enter")
            await pilot.pause()
            assert inbox.pending(seeded_home, "T-2") == []

            await pilot.press("ctrl+d")
            await pilot.pause()
            modal = app.screen_stack[-1]
            confirm = modal.query_one("#confirm-ok")
            assert confirm.disabled
            modal.query_one("#confirm-input", Input).value = "T-3"
            await pilot.pause()
            assert confirm.disabled
            modal.query_one("#confirm-input", Input).value = "T-2"
            await pilot.pause()
            assert not confirm.disabled
            confirm.press()
            await pilot.pause()
            pending = inbox.pending(seeded_home, "T-2")
            assert [c.get("command", c.get("cmd")) for c in pending] == ["discard"]
            assert app._exception is None

    asyncio.run(_inner())


def test_cmd_discard_requires_typed_key(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-2"
            await app.run_action("cmd")
            await pilot.pause()
            cmd_modal = app.screen_stack[-1]
            assert isinstance(cmd_modal, _CmdModal)
            cmd_modal.dismiss(("discard", ""))
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ConfirmModal)
            await pilot.press("escape")
            await pilot.pause()
            assert inbox.pending(seeded_home, "T-2") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_release_refuses_live_claim(seeded_home):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        claims.write_claim(seeded_home, "T-3", proc.pid, "reconcile-T-3")
        claim_file = seeded_home / "derived" / "claims" / "T-3.json"

        async def _live():
            app = _make_app(seeded_home)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app._selected_key = "T-3"
                await pilot.press("z")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert not isinstance(app.screen_stack[-1], _ConfirmModal)
                msgs = [n.message for n in app._notifications]
                assert any("live" in m and str(proc.pid) in m for m in msgs)
                assert claim_file.exists() and proc.poll() is None
                assert app._exception is None

        asyncio.run(_live())
    finally:
        proc.kill()
        proc.wait()

    async def _stale():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await pilot.press("z")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _ConfirmModal)
            await pilot.press("y")
            await pilot.pause()
            assert not claim_file.exists()
            assert app._exception is None

    asyncio.run(_stale())
