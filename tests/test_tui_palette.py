"""Mounted-app tests for the command palette (T-162)."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable

from maestro import store
from maestro.tui import FleetScreen, MaestroTUI, SpecScreen
from tui_support import _filter_idx, _make_app


# --------------------------------------------------------------------------- #
# T-162: command palette                                                       #
# --------------------------------------------------------------------------- #

def _palette_labels(app) -> list[str]:
    from textual.command import CommandList
    lst = app.screen.query_one(CommandList)
    return [str(lst.get_option_at_index(i).prompt) for i in range(lst.option_count)]


async def _palette(app, pilot, key, text=""):
    await pilot.press(key)
    await pilot.pause()
    if text:
        await pilot.press(*text)
    for _ in range(100):  # results arrive asynchronously; wait for them under load
        await pilot.pause(0.05)
        if _palette_labels(app) and (not text or text.lower()[:3] in _palette_labels(app)[0].lower()):
            break


def test_palette_jumps_to_ticket_widening_filter(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._filter_idx == _filter_idx("needs-you")
            assert "T-5" not in [str(k.value) for k in app.query_one(DataTable).rows]
            await _palette(app, pilot, "colon", "T-5")
            await pilot.press("enter")
            await pilot.pause(0.5)
            table = app.query_one(DataTable)
            assert app._selected_key == "T-5"
            assert app._filter_idx == _filter_idx("all")
            assert str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value) == "T-5"
            assert len(app.screen_stack) == 1
            assert app._exception is None
    asyncio.run(_inner())


def test_palette_jump_keeps_filter_when_visible(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = app._filter_idx
            await _palette(app, pilot, "colon", "T-1")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert app._selected_key == "T-1"
            assert app._filter_idx == before
            assert app._exception is None
    asyncio.run(_inner())


def test_palette_runs_app_action(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _palette(app, pilot, "ctrl+p", "Fleet")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, FleetScreen)
            assert app._exception is None
    asyncio.run(_inner())


def test_palette_ticket_subhits_push_screens(seeded_home):
    store.atomic_write(store.ticket_dir(seeded_home, "T-3") / "proposal.md", "# p\n")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._refresh_now()
            await _palette(app, pilot, "colon", "T-3")
            labels = _palette_labels(app)
            assert any("T-3 › Proposal" in lbl for lbl in labels)
            assert "T-3 ready" not in labels[0] and labels[0].startswith("T-3")
            await pilot.press("escape")
            await pilot.pause()
            await _palette(app, pilot, "colon", "T-4")
            assert not any("T-4 › Proposal" in lbl for lbl in _palette_labels(app))
            await pilot.press("escape")
            await pilot.pause()
            await _palette(app, pilot, "colon", "T-3 Spec")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, SpecScreen)
            assert app.screen._key == "T-3"
            assert app._exception is None
    asyncio.run(_inner())


def test_palette_actions_respect_check_action(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen, FleetScreen)
            await _palette(app, pilot, "ctrl+p")
            labels = _palette_labels(app)
            assert not any(lbl.startswith("Answer") for lbl in labels), labels
            assert any(lbl.startswith("Sweep") for lbl in labels), labels
            assert any(lbl.startswith("Pause/Resume") for lbl in labels), labels
            assert not any(lbl.startswith("Quit") and "(q)" in lbl for lbl in labels)
            assert app._exception is None
    asyncio.run(_inner())


def test_palette_discover_lists_hidden_actions_first(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _palette(app, pilot, "ctrl+p")
            labels = _palette_labels(app)
            assert any(lbl.startswith("Fleet (F)") for lbl in labels), labels
            assert any(lbl.startswith("Refresh (r)") for lbl in labels), labels
            assert labels.index(next(l for l in labels if l.startswith("Fleet (F)"))) < \
                labels.index(next(l for l in labels if l.startswith("Refresh (r)")))
            assert app._exception is None
    asyncio.run(_inner())


def test_palette_bindings_hidden_and_both_keys():
    from textual.binding import Binding
    pal = {b.key: b for b in MaestroTUI.BINDINGS if isinstance(b, Binding) and b.action == "command_palette"}
    assert set(pal) == {"colon", "ctrl+p"}
    assert all(not b.show for b in pal.values())
