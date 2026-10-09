"""Mounted-app tests for the live Now column, running filter and `j` jump (T-163)."""
from __future__ import annotations

import asyncio
import os as _os
import subprocess
import sys
import threading as _threading
import time as _time

from textual.widgets import DataTable, Static

from maestro import claims, config as config_mod, snapshot as snap_mod
from tui_support import _claim, _filter_idx, _make_app


async def _live_tick(app, pilot):
    """Run one real worker refresh to completion and let its re-render land."""
    app._kick_live()
    await app.workers.wait_for_complete()
    await pilot.pause()


def _now_cell(app, key):
    table = app.query_one("#tickets", DataTable)
    return str(table.get_row(key)[-1])


def test_now_column_shows_live_claim(seeded_home, tmp_path):
    _claim(seeded_home, "T-3", tmp_path)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            await _live_tick(app, pilot)
            assert _now_cell(app, "T-3").startswith("●")
            assert "running(1)" in str(app.query_one("#filter-bar", Static).content)
            assert claims.read_claim(seeded_home, "T-3")
            assert app._exception is None

    asyncio.run(_inner())


def test_j_jumps_to_running_ticket(seeded_home, tmp_path):
    _claim(seeded_home, "T-3", tmp_path)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await _live_tick(app, pilot)
            assert app._filter_idx == _filter_idx("needs-you")
            await pilot.press("j")
            await pilot.pause()
            assert app._selected_key == "T-3"
            assert app._filter_idx == _filter_idx("all")
            table = app.query_one("#tickets", DataTable)
            assert str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value) == "T-3"
            idx = app._filter_idx
            await pilot.press("j")
            await pilot.pause()
            assert app._filter_idx == idx and app._selected_key == "T-3"
            assert app._exception is None

    asyncio.run(_inner())


def test_now_column_silence_thresholds_and_one_toast(seeded_home, tmp_path):
    log = _claim(seeded_home, "T-3", tmp_path)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            now = _time.time()
            _os.utime(log, (now - 150, now - 150))
            await _live_tick(app, pilot)
            cell = app.query_one("#tickets", DataTable).get_row("T-3")[-1]
            assert "silent" in str(cell) and "yellow" in str(cell.style)
            timeout = config_mod.load(str(seeded_home)).no_output_timeout
            _os.utime(log, (now - timeout * 0.6, now - timeout * 0.6))
            before = len(app._notifications)
            await _live_tick(app, pilot)
            await _live_tick(app, pilot)
            cell = app.query_one("#tickets", DataTable).get_row("T-3")[-1]
            assert "silent" in str(cell) and "red" in str(cell.style)
            toasts = [n for n in list(app._notifications)[before:] if "T-3" in n.message]
            assert len(toasts) == 1
            assert app._exception is None

    asyncio.run(_inner())


def test_now_column_dispatcher_owned_claims(seeded_home, tmp_path):
    _claim(seeded_home, "T-3", tmp_path, kind="testrun", with_log=False)
    _claim(seeded_home, "T-4", tmp_path, kind="restack", with_log=False)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            await _live_tick(app, pilot)
            assert _now_cell(app, "T-3") == "◌ tests"
            assert _now_cell(app, "T-4") == "◌ restack"
            assert "running(2)" in str(app.query_one("#filter-bar", Static).content)

    asyncio.run(_inner())


def test_live_worker_never_releases_claims(seeded_home, tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    _claim(seeded_home, "T-3", tmp_path, pid=proc.pid)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            for _ in range(3):
                await _live_tick(app, pilot)
            assert "running(0)" in str(app.query_one("#filter-bar", Static).content)
            assert _now_cell(app, "T-3") == ""
            assert claims.read_claim(seeded_home, "T-3")
            assert app._exception is None

    asyncio.run(_inner())


def test_live_probe_runs_in_worker(seeded_home, tmp_path, monkeypatch):
    _claim(seeded_home, "T-3", tmp_path)
    main = _threading.get_ident()
    calls: list[tuple[str, int, bool]] = []
    in_populate = {"on": False}

    def _wrap(mod, name):
        real = getattr(mod, name)

        def _w(*a, **kw):
            calls.append((name, _threading.get_ident(), in_populate["on"]))
            return real(*a, **kw)
        monkeypatch.setattr(mod, name, _w)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            _wrap(claims, "probe_processes")
            _wrap(snap_mod, "load")
            real_populate = app._populate

            def _populate():
                in_populate["on"] = True
                try:
                    real_populate()
                finally:
                    in_populate["on"] = False
            app._populate = _populate
            for _ in range(3):
                await _live_tick(app, pilot)
            names = {c[0] for c in calls}
            assert names == {"probe_processes", "load"}
            assert not any(c[2] for c in calls), "disk/ps read while _populate ran"
            assert all(c[1] != main for c in calls), "refresh-driven read on the UI thread"
            assert app._exception is None

    asyncio.run(_inner())
