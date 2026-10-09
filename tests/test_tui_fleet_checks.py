"""Mounted-app tests for the FleetScreen doctor checks table and remedies (T-170)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import DataTable

from maestro import (
    backup as backup_mod,
    claims,
    config as config_mod,
    event_log,
    inbox,
    ops as ops_mod,
)
from maestro.tui import DetailScreen, FleetScreen, _CheckModal, _ConfirmModal
from tui_support import _BINDING_CLASSES, _make_app, _set_deps, _tree_bytes


# --------------------------------------------------------------------------- #
# T-170: FleetScreen doctor checks table + remedies                           #
# --------------------------------------------------------------------------- #

async def _open_fleet_checks(app, pilot):
    await pilot.pause()
    await app.run_action("fleet_panel")
    await pilot.pause()
    assert isinstance(app.screen_stack[-1], FleetScreen)
    await app.workers.wait_for_complete()
    await pilot.pause()
    return app.screen.query_one("#fleet-checks", DataTable)


def _check_rows(table):
    return [(str(table.get_row(r.key)[0]), str(table.get_row(r.key)[1])) for r in table.ordered_rows]


async def _select_check(pilot, table, name):
    table.move_cursor(row=table.get_row_index(name))
    await pilot.pause()


async def _choose_remedy(app, pilot, option_id):
    await pilot.pause()
    assert isinstance(app.screen_stack[-1], _CheckModal)
    picker = app.screen.query_one("#check-remedies")
    picker.highlighted = picker.get_option_index(option_id)
    await pilot.press("enter")
    await pilot.pause()


def test_fleet_checks_table_sorted_worst_first(seeded_home):
    _set_deps(seeded_home, "T-4", ["T-5"])
    _set_deps(seeded_home, "T-5", ["T-4"])

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 50)) as pilot:
            table = await _open_fleet_checks(app, pilot)
            rows = _check_rows(table)
            assert ("fail", "depends_on") in rows
            assert "ok" not in {s for s, _ in rows}
            order = [s for s, _ in rows]
            assert order == sorted(order, key=["fail", "warn"].index)
            await pilot.press("o")
            await pilot.pause()
            rows = _check_rows(table)
            total = len(app.screen._doctor["checks"])
            assert len(rows) == total == len(__import__("maestro.health").health.CHECKS)
            order = [s for s, _ in rows]
            assert order == sorted(order, key=["fail", "warn", "ok"].index)
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_check_dead_letter_retry(seeded_home):
    cfg = config_mod.load(str(seeded_home))
    ops_mod.fail(cfg, "T-2", "boom", dead_letter=True)
    before = event_log.read(seeded_home, "T-2")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 50)) as pilot:
            table = await _open_fleet_checks(app, pilot)
            await _select_check(pilot, table, "dead_letters")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _CheckModal)
            assert "dead-lettered" in str(app.screen.query_one("#check-json").render())
            await _choose_remedy(app, pilot, "retry|T-2")
            assert [c["command"] for c in inbox.pending(seeded_home, "T-2")] == ["retry"]
            assert app._exception is None

    asyncio.run(_inner())
    assert event_log.read(seeded_home, "T-2") == before


def test_fleet_check_dead_letter_discard_typed_confirm(seeded_home):
    cfg = config_mod.load(str(seeded_home))
    ops_mod.fail(cfg, "T-2", "boom", dead_letter=True)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 50)) as pilot:
            table = await _open_fleet_checks(app, pilot)
            await _select_check(pilot, table, "dead_letters")
            await pilot.press("enter")
            await _choose_remedy(app, pilot, "discard|T-2")
            assert isinstance(app.screen_stack[-1], _ConfirmModal)
            await pilot.press("enter")  # empty input: nothing happens
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _ConfirmModal)
            assert inbox.pending(seeded_home, "T-2") == []
            for ch in "T-2":
                await pilot.press(ch)
            await pilot.pause()
            app.screen.query_one("#confirm-ok").press()
            await pilot.pause()
            assert [c["command"] for c in inbox.pending(seeded_home, "T-2")] == ["discard"]
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_check_g_opens_culprit_detail(seeded_home):
    cfg = config_mod.load(str(seeded_home))
    ops_mod.fail(cfg, "T-2", "boom", dead_letter=True)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 50)) as pilot:
            table = await _open_fleet_checks(app, pilot)
            assert dict(((n, s) for s, n in _check_rows(table)))["backup_age"] == "warn"
            await _select_check(pilot, table, "backup_age")
            depth = len(app.screen_stack)
            await pilot.press("g")
            await pilot.pause()
            assert len(app.screen_stack) == depth and isinstance(app.screen, FleetScreen)
            assert any("no ticket" in n.message for n in app._notifications)
            await _select_check(pilot, table, "dead_letters")
            await pilot.press("g")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            assert app.screen_stack[-1]._key == "T-2"
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_check_release_refuses_live_claim(seeded_home, child_process):
    claims.write_claim(seeded_home, "T-3", child_process.pid, "reconcile-T-3")
    claim_file = seeded_home / "derived" / "claims" / "T-3.json"

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 50)) as pilot:
            table = await _open_fleet_checks(app, pilot)
            await pilot.press("o")
            await pilot.pause()
            await _select_check(pilot, table, "claim_age")
            await pilot.press("enter")
            await _choose_remedy(app, pilot, "release|T-3")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not isinstance(app.screen_stack[-1], _ConfirmModal)
            assert any("live" in n.message for n in app._notifications)
            assert claim_file.exists() and child_process.poll() is None
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_check_backup_now_never_restores(seeded_home):
    cfg = config_mod.load(str(seeded_home))
    before_backups = len(backup_mod.list_backups(cfg))
    before = _tree_bytes(seeded_home, "events")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 50)) as pilot:
            table = await _open_fleet_checks(app, pilot)
            await _select_check(pilot, table, "backup_age")
            await pilot.press("enter")
            await _choose_remedy(app, pilot, "backup-now|")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    assert len(backup_mod.list_backups(cfg)) == before_backups + 1
    assert _tree_bytes(seeded_home, "events") == before
    for path in sorted((Path(__file__).parent.parent / "maestro" / "tui").rglob("*.py")):
        assert "restore_backup" not in path.read_text(encoding="utf-8"), path.name
    assert _CheckModal in _BINDING_CLASSES
