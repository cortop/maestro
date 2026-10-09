"""Mounted-app tests for the home banner and Backups screen (T-160)."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from textual.widgets import DataTable

from maestro.tui.modals import _TextViewModal
from maestro import backup as backup_mod, config as config_mod
from maestro.tui import BackupsScreen, FleetScreen, _ConfirmModal
from tui_support import _BINDING_CLASSES, _make_app, _tree_bytes


# --------------------------------------------------------------------------- #
# T-160: home + backup-age banner, Backups screen                              #
# --------------------------------------------------------------------------- #

def _seed_backup(home, age_s: float) -> Path:
    return backup_mod.create_backup(config_mod.load(str(home)), time.time() - age_s)


async def _banner(app, pilot) -> str:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()
    return app.sub_title


def test_home_banner_shows_home_and_backup_age(seeded_home):
    _seed_backup(seeded_home, 120)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            sub = await _banner(app, pilot)
            assert str(seeded_home.resolve()) in sub
            assert "backup 2m old" in sub
            assert "⚠" not in sub
            assert app._exception is None

    asyncio.run(_inner())


def test_home_banner_warns(seeded_home, tmp_path_factory, monkeypatch):
    async def _sub(home) -> str:
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            sub = await _banner(app, pilot)
            assert app._exception is None
            return sub

    # no tarball, events present
    assert "⚠ no backup" in asyncio.run(_sub(seeded_home))
    # newest tarball older than 2x backup_interval (default 3600s)
    _seed_backup(seeded_home, 3 * 3600)
    assert "⚠ backup 3h old" in asyncio.run(_sub(seeded_home))
    # fresh backup on a non-default home: clean
    _seed_backup(seeded_home, 60)
    assert "⚠" not in asyncio.run(_sub(seeded_home))
    # phantom default home: HOME=<tmp>, TUI on <tmp>/.maestro with an empty events/
    fake = tmp_path_factory.mktemp("fakehome")
    monkeypatch.setenv("HOME", str(fake))
    phantom = fake / ".maestro"
    for d in ("events", "inbox", "tickets", "derived"):
        (phantom / d).mkdir(parents=True)
    (phantom / "config.toml").write_text("[maestro]\n", encoding="utf-8")
    assert "⚠ empty default home" in asyncio.run(_sub(phantom))


async def _open_backups(app, pilot):
    await pilot.pause()
    await pilot.press("F")
    await pilot.pause()
    assert isinstance(app.screen, FleetScreen)
    await pilot.press("b")
    await pilot.pause()
    assert isinstance(app.screen, BackupsScreen)
    await app.workers.wait_for_complete()
    await pilot.pause()
    return app.screen.query_one(DataTable)


def test_backups_screen_lists_tarballs(seeded_home):
    older = _seed_backup(seeded_home, 7200)
    newer = _seed_backup(seeded_home, 60)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            table = await _open_backups(app, pilot)
            rows = [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]
            assert [r[0] for r in rows] == [newer.name, older.name]
            assert rows[0][1] == "1m" and rows[1][1] == "2h"
            assert all(r[2].endswith(("B", "KB", "MB")) for r in rows)
            assert app._exception is None

    asyncio.run(_inner())


def test_backups_screen_creates_backup(seeded_home):
    bdir = backup_mod.resolve_backup_dir(config_mod.load(str(seeded_home)))
    before = _tree_bytes(seeded_home, "events")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            table = await _open_backups(app, pilot)
            assert table.row_count == 0
            await pilot.press("b")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(list(bdir.glob("maestro-backup-*.tar.gz"))) == 1
            assert table.row_count == 1
            assert app._exception is None

    asyncio.run(_inner())
    assert _tree_bytes(seeded_home, "events") == before


def test_backup_prune_requires_confirm(seeded_home):
    (seeded_home / "config.toml").write_text("[maestro]\nbackup_retention = 2\n", encoding="utf-8")
    old = _seed_backup(seeded_home, 7200)
    mid = _seed_backup(seeded_home, 3600)
    bdir = old.parent

    def _names():
        return sorted(p.name for p in bdir.glob("maestro-backup-*.tar.gz"))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open_backups(app, pilot)
            await pilot.press("b")
            await pilot.pause()
            assert isinstance(app.screen, _ConfirmModal)
            assert old.name in str(app.screen._message) and mid.name not in str(app.screen._message)
            await pilot.press("enter")  # default focus is Cancel
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert _names() == sorted([old.name, mid.name])
            await pilot.press("b")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            names = _names()
            assert len(names) == 2 and old.name not in names and mid.name in names
            assert app._exception is None

    asyncio.run(_inner())


def test_backups_screen_lists_members(seeded_home):
    _seed_backup(seeded_home, 60)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open_backups(app, pilot)
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, _TextViewModal)
            members = app.screen.text.splitlines()
            assert "events" in members and "tickets" in members
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, BackupsScreen)
            assert app._exception is None

    asyncio.run(_inner())


def test_backups_screen_copies_restore_command(seeded_home):
    tarball = _seed_backup(seeded_home, 60)
    before = _tree_bytes(seeded_home, "events", "tickets", "inbox")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open_backups(app, pilot)
            await pilot.press("y")
            await pilot.pause()
            assert app.clipboard == f"maestro --home {seeded_home} restore {tarball}"
            assert app._exception is None

    asyncio.run(_inner())
    assert _tree_bytes(seeded_home, "events", "tickets", "inbox") == before


def test_tui_never_restores():
    for path in sorted((Path(__file__).parent.parent / "maestro" / "tui").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        assert "restore_backup" not in src and "cmd_restore" not in src, path.name
    assert BackupsScreen in _BINDING_CLASSES
