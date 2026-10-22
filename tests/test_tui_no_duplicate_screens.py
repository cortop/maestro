"""A key that opens the screen already on top is a no-op; Esc pops one level at a time."""
from __future__ import annotations

import asyncio

import pytest
from tui_support import _make_app

from maestro.tui.screens import ActivityScreen
from maestro.tui.screens import AcScreen
from maestro.tui.screens import BackupsScreen
from maestro.tui.screens import DecisionsScreen
from maestro.tui.screens import DepsScreen
from maestro.tui.screens import EnvScreen
from maestro.tui.screens import EventsScreen
from maestro.tui.screens import FleetScreen
from maestro.tui.screens import InboxScreen
from maestro.tui.screens import LogsScreen
from maestro.tui.screens import ProposalScreen
from maestro.tui.screens import ReviewScreen
from maestro.tui.screens import ScheduleScreen
from maestro.tui.screens import SessionsScreen
from maestro.tui.screens import SpecScreen
from maestro.tui.screens import WhyScreen

# (key, screen it opens) for every full-screen-opening board binding
SCREEN_KEYS = [
    ("s", SpecScreen), ("l", LogsScreen), ("v", AcScreen), ("w", WhyScreen),
    ("I", InboxScreen), ("D", DepsScreen), ("F", FleetScreen), ("B", SessionsScreen),
    ("R", ReviewScreen), ("T", ActivityScreen), ("S", ScheduleScreen), ("e", EnvScreen),
    ("W", DecisionsScreen),
]


def _run(seeded_home, body):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await body(app, pilot)
            assert app._exception is None
    asyncio.run(_inner())


@pytest.mark.parametrize("key,screen_type", SCREEN_KEYS)
def test_repeat_key_pushes_nothing(seeded_home, key, screen_type):
    async def body(app, pilot):
        base = len(app.screen_stack)
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, screen_type)
        assert len(app.screen_stack) == base + 1
        await pilot.press(key)
        await pilot.pause()
        assert len(app.screen_stack) == base + 1
        assert isinstance(app.screen, screen_type)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == base
        assert not isinstance(app.screen, screen_type)
    _run(seeded_home, body)


def test_spec_repeat_then_single_escape_returns_to_board(seeded_home):
    async def body(app, pilot):
        board = app.screen
        await pilot.press("s")
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is board
    _run(seeded_home, body)


def test_different_screens_still_stack_one_escape_each(seeded_home):
    async def body(app, pilot):
        board = app.screen
        await pilot.press("s")
        await pilot.pause()
        spec = app.screen
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, LogsScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is spec
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is board
    _run(seeded_home, body)


# (keys leading to the opener screen, key that opens screen_type from there, screen it opens)
NESTED_KEYS = [
    (["F"], "b", BackupsScreen),
    (["enter"], "v", EventsScreen),
    (["enter"], "p", ProposalScreen),
]


@pytest.mark.parametrize("path,key,screen_type", NESTED_KEYS)
def test_repeat_nested_key_pushes_nothing(seeded_home, path, key, screen_type):
    for d in (seeded_home / "tickets").iterdir():
        (d / "proposal.md").write_text("# Proposal\n\n## Recommendation\nship it\n")

    async def body(app, pilot):
        board = app.screen
        for k in path:
            await pilot.press(k)
            await pilot.pause()
        opener = app.screen
        base = len(app.screen_stack)
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, screen_type)
        assert len(app.screen_stack) == base + 1
        # the opener's own action fires again on the top screen: still nothing pushed
        app.push_screen(type(app.screen)(*_ctor_args(app.screen)))
        await pilot.pause()
        assert len(app.screen_stack) == base + 1
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is opener
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is board
    _run(seeded_home, body)


def _ctor_args(screen):
    key = getattr(screen, "_key", None)
    return (screen._home,) if key is None else (screen._home, key)
