"""Mounted-app tests for `b` blocker/dependent hops and `O` open-PR (T-158)."""
from __future__ import annotations

import asyncio

import pytest

from conftest import seed_ticket
from maestro.tui import DetailScreen, SpecScreen, _TicketPickModal
from tui_support import _make_app, _set_deps


@pytest.fixture
def dep_home(seeded_home):
    seed_ticket(seeded_home, "T-6", "depends on T-5", phase="ready")
    _set_deps(seeded_home, "T-6", ["T-5"])
    return seeded_home


async def _pick(app, pilot, opt_id):
    for _ in range(40):
        await pilot.pause(0.05)
        if isinstance(app.screen, _TicketPickModal):
            break
    assert isinstance(app.screen, _TicketPickModal)
    picker = app.screen.query_one("#ticket-pick")
    ids = [picker.get_option_at_index(i).id for i in range(picker.option_count)]
    return picker, ids


def test_b_hops_to_blocker_in_place(dep_home):
    async def _inner():
        app = _make_app(dep_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(DetailScreen(dep_home, "T-6"))
            await pilot.pause()
            depth = len(app.screen_stack)
            await pilot.press("b")
            picker, ids = await _pick(app, pilot, "T-5")
            assert "T-5" in ids
            picker.highlighted = ids.index("T-5")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen) and app.screen._key == "T-5"
            assert len(app.screen_stack) == depth
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert app._exception is None

    asyncio.run(_inner())


def test_b_lists_dependents_done_and_missing(dep_home):
    seed_ticket(dep_home, "T-7", "done dep", phase="done")
    seed_ticket(dep_home, "T-8", "has done and missing deps", phase="ready")
    _set_deps(dep_home, "T-8", ["T-7", "T-99"])

    async def _inner():
        app = _make_app(dep_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(DetailScreen(dep_home, "T-5"))
            await pilot.pause()
            await pilot.press("b")
            _, ids = await _pick(app, pilot, "T-6")
            assert "T-6" in ids
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            app.push_screen(DetailScreen(dep_home, "T-8"))
            await pilot.pause()
            depth = len(app.screen_stack)
            await pilot.press("b")
            picker, ids = await _pick(app, pilot, "T-7")
            assert "T-7" in ids and "T-99" in ids
            picker.highlighted = ids.index("T-99")
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == depth
            assert isinstance(app.screen, DetailScreen) and app.screen._key == "T-8"
            assert any("T-99" in n.message for n in app._notifications)
            assert app._exception is None

    asyncio.run(_inner())


def test_b_notifies_when_nothing_to_hop_to(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, _TicketPickModal)
            assert any("No blockers or dependents" in n.message for n in app._notifications)

    asyncio.run(_inner())


def test_b_targets_visible_ticket(dep_home):
    async def _inner():
        app = _make_app(dep_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            app.push_screen(SpecScreen(dep_home, "T-6"))
            await pilot.pause()
            await pilot.press("b")
            picker, ids = await _pick(app, pilot, "T-5")
            assert ids.count("T-5") == 1 and "T-3" not in ids
            picker.highlighted = ids.index("T-5")
            depth = len(app.screen_stack) - 1  # minus the picker modal
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SpecScreen) and app.screen._key == "T-5"
            assert len(app.screen_stack) == depth
            assert app._exception is None

    asyncio.run(_inner())


def test_O_opens_pr_url(seeded_home, opened_urls):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await pilot.press("O")
            await pilot.pause()
            assert opened_urls == ["https://github.com/cortop/maestro/pull/15"]
            app._selected_key = "T-5"
            await pilot.press("O")
            await pilot.pause()
            assert opened_urls == ["https://github.com/cortop/maestro/pull/15"]
            assert any("No PR" in n.message for n in app._notifications)
            assert app._exception is None

    asyncio.run(_inner())


def test_webbrowser_open_is_guarded(opened_urls):
    import webbrowser
    webbrowser.open("https://example.invalid")
    assert opened_urls == ["https://example.invalid"]
