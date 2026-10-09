"""Mounted-app tests for `K` -> _StopModal -> stop_session (T-176)."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading as _threading
import time as _time

import pytest
from textual.widgets import Checkbox

from maestro import claims, inbox
from maestro.tui import _StopModal
from tui_support import _capture_toasts, _make_app, _settle


# --------------------------------------------------------------------------- #
# T-176: K -> _StopModal (Cancel default) -> ops.stop_session in a worker      #
# --------------------------------------------------------------------------- #

def _live_session(home, key="T-3"):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    _threading.Thread(target=proc.wait, daemon=True).start()  # we are the parent: reap
    claims.write_claim(home, key, proc.pid, f"reconcile-{key}")
    return proc


async def _wait_modal(app, pilot, cls, timeout=10.0):
    """The K probe runs `ps` in a worker; under load the modal lands a beat late."""
    deadline = _time.time() + timeout
    while not isinstance(app.screen, cls) and _time.time() < deadline:
        await _settle(app, pilot)


async def _wait_gone(proc, timeout=5.0):
    deadline = _time.time() + timeout
    while proc.poll() is None and _time.time() < deadline:
        await asyncio.sleep(0.05)


def test_stop_modal_defaults_to_cancel_then_stops(seeded_home):
    proc = _live_session(seeded_home)

    async def _inner():
        app = _make_app(seeded_home)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                toasts = _capture_toasts(app)
                app._selected_key = "T-3"
                await pilot.press("K")
                await _wait_modal(app, pilot, _StopModal)
                assert isinstance(app.screen, _StopModal)
                text = " ".join(str(w.render()) for w in app.screen.query("Label"))
                assert str(proc.pid) in text and "confirmed" in text
                await pilot.press("enter")  # Cancel is focused
                await pilot.pause()
                assert not isinstance(app.screen, _StopModal)
                assert proc.poll() is None
                await pilot.press("K")
                await _wait_modal(app, pilot, _StopModal)
                await pilot.click("#stop-ok")
                await _settle(app, pilot)
                await _wait_gone(proc)
                await _settle(app, pilot)
                assert proc.poll() is not None
                assert any("stopped" in t for t in toasts), toasts
                assert app._exception is None
        finally:
            if proc.poll() is None:
                proc.kill()

    asyncio.run(_inner())


def test_stop_warns_without_stoppable_claim(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            toasts = _capture_toasts(app)
            app._selected_key = "T-3"
            await pilot.press("K")
            await _settle(app, pilot)
            assert not isinstance(app.screen, _StopModal)
            claims.write_claim(seeded_home, "T-3", 1, "x", kind="testrun")
            await pilot.press("K")
            await _settle(app, pilot)
            assert not isinstance(app.screen, _StopModal)
            assert len(toasts) == 2 and app._exception is None

    asyncio.run(_inner())


@pytest.mark.parametrize("checked", [False, True])
def test_stop_note_and_nudge(seeded_home, checked):
    proc = _live_session(seeded_home)

    async def _inner():
        app = _make_app(seeded_home)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app._selected_key = "T-3"
                before = len(inbox.pending(seeded_home, "T-3"))
                await pilot.press("K")
                await _wait_modal(app, pilot, _StopModal)
                if checked:
                    app.screen.query_one("#stop-nudge", Checkbox).value = True
                await pilot.click("#stop-ok")
                await _settle(app, pilot)
                await _wait_gone(proc)
                await _settle(app, pilot)
                await _settle(app, pilot)
                msgs = inbox.pending(seeded_home, "T-3")
                spawned = [s[0] for s in app.dry.spawned]
                if checked:
                    assert len(msgs) == before + 1 and msgs[-1]["command"] == "msg"
                    assert spawned == ["T-3"]
                else:
                    assert len(msgs) == before and spawned == []
                assert app._exception is None
        finally:
            if proc.poll() is None:
                proc.kill()

    asyncio.run(_inner())
