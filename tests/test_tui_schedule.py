"""Mounted-app tests for ScheduleScreen and the schedule modal."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input, TextArea

from maestro import config as config_mod
from maestro.tui import ScheduleScreen, _ScheduleModal
from tui_support import _make_app, _open_via_action


# --- T-10: scheduled tasks TUI surface ---------------------------------------

def _write_scheduled_config(home, **overrides):
    task = {
        "name": "digest", "prompt": "Summarize things", "every": "1h",
        "kind": "implementation", "priority": 3,
        "prefix": "S", "enabled": True,
    }
    task.update(overrides)
    config_mod.write_scheduled(home, [task])
    return task


def test_schedule_screen_open_and_escape(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _open_via_action(app, pilot, "schedule_panel", ScheduleScreen)

    asyncio.run(_inner())


def test_schedule_screen_shows_configured_tasks(seeded_home):
    _write_scheduled_config(seeded_home)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            screen = app.screen_stack[-1]
            assert isinstance(screen, ScheduleScreen)
            table = screen.query_one("#schedule-table", DataTable)
            assert table.row_count == 1
            assert app._exception is None

    asyncio.run(_inner())


def test_schedule_modal_open_and_escape(seeded_home):
    _write_scheduled_config(seeded_home)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            schedule_screen = app.screen_stack[-1]
            before = len(app.screen_stack)
            await schedule_screen.run_action("add_task")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _ScheduleModal)
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == before

    asyncio.run(_inner())


def test_schedule_modal_add_task_writes_config(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            schedule_screen = app.screen_stack[-1]
            await schedule_screen.run_action("add_task")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ScheduleModal)
            modal.query_one("#sched-name", Input).value = "new-task"
            modal.query_one("#sched-prompt", TextArea).text = "Do the thing"
            modal.query_one("#sched-every", Input).value = "6h"
            await modal.run_action("submit")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    cfg = config_mod.load(str(seeded_home))
    assert len(cfg.scheduled) == 1
    assert cfg.scheduled[0]["name"] == "new-task"
    assert cfg.scheduled[0]["every"] == "6h"


# --- GA-9: title/repo round-trip through add/edit/toggle, mounted app -------

def test_schedule_add_edit_toggle_roundtrips_title_and_repo(seeded_home):
    """GA-9 mounted-app QA: drives the real ScheduleScreen through add -> edit
    -> toggle via real key presses, asserting title/repo survive each step and
    that toggling one task never strips another task's fields (blast radius).
    GA-13: these three actions now call `ops.schedule_add`/`schedule_edit`/
    `schedule_set_enabled` instead of inlining load -> mutate -> write_scheduled,
    so this same real-key-press walk is this ticket's mounted-app proof that
    the ops refactor didn't change observable behavior."""
    _write_scheduled_config(seeded_home, name="other", title="Other title", repo="beta")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("S")  # schedule_panel
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], ScheduleScreen)

            # --- add: a new titled+repo-bound task -------------------------
            await pilot.press("n")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ScheduleModal)
            modal.query_one("#sched-name", Input).value = "digest"
            modal.query_one("#sched-prompt", TextArea).text = "Summarize things"
            modal.query_one("#sched-every", Input).value = "1h"
            modal.query_one("#sched-title", Input).value = "Morning digest"
            modal.query_one("#sched-repo", Input).value = "alpha"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None

            cfg = config_mod.load(str(seeded_home))
            added = next(t for t in cfg.scheduled if t["name"] == "digest")
            assert added["title"] == "Morning digest"
            assert added["repo"] == "alpha"

            # --- edit: select the new task, change its title, keep repo ----
            screen = app.screen_stack[-1]
            table = screen.query_one("#schedule-table", DataTable)
            for row_idx in range(table.row_count):
                if str(table.get_row_at(row_idx)[0]) == "digest":
                    table.move_cursor(row=row_idx)
                    break
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ScheduleModal)
            assert modal.query_one("#sched-title", Input).value == "Morning digest"
            assert modal.query_one("#sched-repo", Input).value == "alpha"
            modal.query_one("#sched-title", Input).value = "Morning digest (edited)"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None

            cfg = config_mod.load(str(seeded_home))
            edited = next(t for t in cfg.scheduled if t["name"] == "digest")
            assert edited["title"] == "Morning digest (edited)"
            assert edited["repo"] == "alpha"  # untouched field survives the merge

            # --- toggle: flip "digest" and prove "other" is untouched -------
            screen = app.screen_stack[-1]
            table = screen.query_one("#schedule-table", DataTable)
            for row_idx in range(table.row_count):
                if str(table.get_row_at(row_idx)[0]) == "digest":
                    table.move_cursor(row=row_idx)
                    break
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    cfg = config_mod.load(str(seeded_home))
    digest = next(t for t in cfg.scheduled if t["name"] == "digest")
    other = next(t for t in cfg.scheduled if t["name"] == "other")
    assert digest["enabled"] is False
    assert digest["title"] == "Morning digest (edited)"
    assert digest["repo"] == "alpha"
    # blast radius: toggling "digest" must not strip "other"'s fields
    assert other["title"] == "Other title"
    assert other["repo"] == "beta"


def test_schedule_toggle_task_flips_enabled(seeded_home):
    _write_scheduled_config(seeded_home, enabled=True)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            screen = app.screen_stack[-1]
            table = screen.query_one("#schedule-table", DataTable)
            table.move_cursor(row=0)
            await pilot.pause()
            await screen.run_action("toggle_task")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    cfg = config_mod.load(str(seeded_home))
    assert cfg.scheduled[0]["enabled"] is False


def test_schedule_modal_add_cron_task_then_toggle_survives_fields(seeded_home):
    """GA-19 mounted-app QA: drives the real ScheduleScreen through creating a
    cron+tz task, then toggling it, via real key presses -- asserting the cron
    and tz fields survive in config.toml and app._exception stays None
    throughout. No new bindings here (the modal gains Input widgets, not new
    keybindings), so nothing to add to the binding sweep."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("S")  # schedule_panel
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], ScheduleScreen)

            # --- add: a cron+tz task, no 'every' -----------------------------
            await pilot.press("n")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ScheduleModal)
            modal.query_one("#sched-name", Input).value = "weekly-digest"
            modal.query_one("#sched-prompt", TextArea).text = "Summarize the week"
            modal.query_one("#sched-cron", Input).value = "0 9 * * 1"
            modal.query_one("#sched-tz", Input).value = "America/New_York"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None

            cfg = config_mod.load(str(seeded_home))
            added = next(t for t in cfg.scheduled if t["name"] == "weekly-digest")
            assert added["cron"] == "0 9 * * 1"
            assert added["tz"] == "America/New_York"
            assert "every" not in added

            # --- toggle: flip it, cron/tz must survive the merge -------------
            screen = app.screen_stack[-1]
            table = screen.query_one("#schedule-table", DataTable)
            for row_idx in range(table.row_count):
                if str(table.get_row_at(row_idx)[0]) == "weekly-digest":
                    table.move_cursor(row=row_idx)
                    break
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    cfg = config_mod.load(str(seeded_home))
    toggled = next(t for t in cfg.scheduled if t["name"] == "weekly-digest")
    assert toggled["enabled"] is False
    assert toggled["cron"] == "0 9 * * 1"
    assert toggled["tz"] == "America/New_York"


def test_schedule_modal_rejects_both_every_and_cron(seeded_home):
    """GA-19: the modal enforces "exactly one of every/cron" client-side --
    submitting with both filled in must notify (not crash) and leave
    config.toml untouched."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            screen = app.screen_stack[-1]
            await screen.run_action("add_task")
            await pilot.pause()
            modal = app.screen_stack[-1]
            modal.query_one("#sched-name", Input).value = "both"
            modal.query_one("#sched-prompt", TextArea).text = "P"
            modal.query_one("#sched-every", Input).value = "1h"
            modal.query_one("#sched-cron", Input).value = "0 2 * * *"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None
            assert isinstance(app.screen_stack[-1], _ScheduleModal)  # still open

    asyncio.run(_inner())
    assert config_mod.load(str(seeded_home)).scheduled == []


# --- GA-13: TUI schedule verbs route through ops.schedule_* -----------------

def test_schedule_add_duplicate_name_notifies_without_writing(seeded_home):
    """GA-13 AC: the ops-owned duplicate-name check fires through the TUI too --
    submitting the add modal with a name that already exists must notify (not
    crash) and must leave config.toml's one existing task exactly as it was."""
    original = _write_scheduled_config(seeded_home, name="digest", every="1h")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            screen = app.screen_stack[-1]
            await screen.run_action("add_task")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ScheduleModal)
            modal.query_one("#sched-name", Input).value = "digest"  # duplicate
            modal.query_one("#sched-prompt", TextArea).text = "A different prompt"
            modal.query_one("#sched-every", Input).value = "6h"
            notifs_before = len(app._notifications)
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app._notifications) > notifs_before
            # the modal is gone (it always dismisses itself); the screen underneath is live
            assert app.screen_stack[-1] is screen

    asyncio.run(_inner())
    cfg = config_mod.load(str(seeded_home))
    assert cfg.scheduled == [original]  # untouched -- no second task, original fields intact
