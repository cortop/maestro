"""Mounted-app tests for the spec priority/dependsOn modal (`M`, T-179)."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input

from conftest import seed_ticket
from maestro.tui.modals import _SpecFieldsModal
from maestro import store
from maestro.cli import main as cli_main
from tui_support import _filter_idx, _make_app


# --------------------------------------------------------------------------- #
# T-179: spec priority/dependsOn modal ('M')                                  #
# --------------------------------------------------------------------------- #

def _open_spec_fields(app):
    modal = app.screen_stack[-1]
    assert isinstance(modal, _SpecFieldsModal)
    return modal


def test_spec_fields_modal_sets_depends_on(seeded_home):
    from textual.widgets import SelectionList

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            app._selected_key = "T-5"
            await pilot.press("M")
            await pilot.pause()
            modal = _open_spec_fields(app)
            modal.query_one("#spec-deps", SelectionList).select("T-3")
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1
            table = app.query_one("#tickets", DataTable)
            col = [c.label.plain for c in table.columns.values()].index("Deps")
            assert str(table.get_row("T-5")[col]) == "1"

    asyncio.run(_inner())
    assert "dependsOn: [T-3]" in store.spec_path(seeded_home, "T-5").read_text()


def test_spec_fields_modal_blocks_cycle(seeded_home):
    from textual.widgets import Label, SelectionList
    assert cli_main(["--home", str(seeded_home), "spec-set", "T-3", "--depends-on", "T-5"]) == 0
    before = store.spec_path(seeded_home, "T-5").read_bytes()

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await pilot.press("M")
            await pilot.pause()
            modal = _open_spec_fields(app)
            modal.query_one("#spec-deps", SelectionList).select("T-3")
            await pilot.pause()
            assert "CYCLE:" in str(modal.query_one("#spec-preview", Label).render())
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app._exception is None
            assert app.screen_stack[-1] is modal  # Save refused, still open

    asyncio.run(_inner())
    assert store.spec_path(seeded_home, "T-5").read_bytes() == before


def test_spec_fields_modal_keeps_done_deps(seeded_home):
    from textual.widgets import Label
    seed_ticket(seeded_home, "T-9", "finished", phase="done")
    path = store.spec_path(seeded_home, "T-5")
    assert cli_main(["--home", str(seeded_home), "spec-set", "T-5", "--depends-on", "T-9"]) == 0

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await pilot.press("M")
            await pilot.pause()
            modal = _open_spec_fields(app)
            assert "kept: T-9" in str(modal.query_one("#spec-kept", Label).render())
            modal.query_one("#spec-priority", Input).value = "1"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())
    text = path.read_text()
    assert "dependsOn: [T-9]" in text and "priority: 1" in text
