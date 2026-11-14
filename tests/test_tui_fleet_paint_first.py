"""The TUI Fleet tab and the `F` panel paint status before the doctor run returns."""
from __future__ import annotations

import asyncio
import threading

from textual.widgets import Static

from maestro import health
from maestro.tui import FleetScreen
from tui_support import _make_app


def _text(widget: Static) -> str:
    c = widget.content
    return c if isinstance(c, str) else str(c)


def _slow_doctor(monkeypatch):
    gate = threading.Event()
    real = health.report

    def report(cfg, now, **kw):
        gate.wait(10)
        return real(cfg, now, **kw)

    monkeypatch.setattr(health, "report", report)
    return gate


def _until(pilot, pred, tries=100):
    async def _w():
        for _ in range(tries):
            await pilot.pause(0.05)
            if pred():
                return
        raise AssertionError("condition never held")
    return _w()


def test_fleet_tab_paints_status_before_doctor(seeded_home, monkeypatch):
    gate = _slow_doctor(monkeypatch)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("tab_fleet")
            tab = app.query_one("#fleet-tab", Static)
            await _until(pilot, lambda: "Loaded:" in _text(tab))
            assert "running" in _text(tab) and app._doctor_cache is None
            gate.set()
            await _until(pilot, lambda: app._doctor_cache is not None)
            await _until(pilot, lambda: "checks from" in _text(tab))
            assert app._exception is None
    asyncio.run(_inner())


def test_fleet_panel_paints_first_then_reopens_with_cached_checks(seeded_home, monkeypatch):
    gate = _slow_doctor(monkeypatch)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            screen = app.screen_stack[-1]
            assert isinstance(screen, FleetScreen)
            status = screen.query_one("#fleet-status", Static)
            await _until(pilot, lambda: "Loaded:" in _text(status))
            assert app._doctor_cache is None and "running" in _text(status)
            gate.set()
            await _until(pilot, lambda: app._doctor_cache is not None)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "checks from" in _text(status)
            await pilot.press("escape")
            await pilot.pause()
            # Reopen with a doctor run that never returns: the cached checks show at once.
            gate.clear()
            await app.run_action("fleet_panel")
            await pilot.pause()
            screen = app.screen_stack[-1]
            assert isinstance(screen, FleetScreen)
            assert "Doctor checks:" in _text(screen.query_one("#fleet-checks-header", Static))
            await _until(pilot, lambda: "checks from" in _text(screen.query_one("#fleet-status", Static)))
            assert "refreshing" in _text(screen.query_one("#fleet-status", Static))
            gate.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "refreshing" not in _text(screen.query_one("#fleet-status", Static))
            assert app._exception is None
    asyncio.run(_inner())
