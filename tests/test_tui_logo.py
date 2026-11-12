"""Mask logo on the Now tab's empty state and in the help panel (T-257)."""
from __future__ import annotations

import asyncio

from textual.widgets import HelpPanel

from conftest import seed_ticket
from maestro.tui.render import MASK_LOGO, NEON_LEDGER, _render_mask_logo
from maestro.tui.screens import MaskLogo
from tui_support import _make_app

LINE = "Nothing needs you"


def _run(home, body, size=(140, 40)):
    async def _inner():
        app = _make_app(home)
        app._nudge_enabled = False
        async with app.run_test(size=size) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await body(app, pilot)
            assert app._exception is None
    asyncio.run(_inner())


def _text(widget) -> str:
    return widget.render().plain


def test_now_tab_shows_mask_when_nothing_needs_human_and_hides_it_when_something_does(home):
    async def empty(app, pilot):
        logo = app.query_one("#now-logo", MaskLogo)
        assert logo.display
        assert all(row.strip() in _text(logo) for row in MASK_LOGO)
        assert LINE in _text(logo)
    _run(home, empty)

    seed_ticket(home, "Q-1", "needs an answer", phase="awaiting-human",
                questions={"q1": "Pick it up?\n   Recommended: Yes"})

    async def needed(app, pilot):
        assert not app.query_one("#now-logo", MaskLogo).display
    _run(home, needed)


def test_help_panel_shows_mask_above_keys(home):
    async def body(app, pilot):
        await pilot.press("question_mark")
        await pilot.pause()
        panel = app.screen.query_one(HelpPanel)
        logo = panel.query_one("#help-logo", MaskLogo)
        assert all(row.strip() in _text(logo) for row in MASK_LOGO)
        assert logo.region.y < panel.query_one("#keys-help").region.y
    _run(home, body)


def test_mask_colours_come_from_theme():
    text = _render_mask_logo(NEON_LEDGER.primary, NEON_LEDGER.secondary)
    styles = {}
    for r, row in enumerate(text.plain.split("\n")):
        for c, ch in enumerate(row):
            if ch != " ":
                styles.setdefault(str(text.get_style_at_offset(__import__("rich").get_console(), sum(len(x) + 1 for x in text.plain.split("\n")[:r]) + c)), set()).add((r, ch))
    gold = next(s for s in styles if "ebcb55" in s.lower())
    cyan = next(s for s in styles if "00e5ff" in s.lower())
    assert {r for r, _ in styles[cyan]} == {3}
    assert {ch for _, ch in styles[cyan]} <= {"▀", "▄"}
    assert {0, 1, 2, 4, 5, 6, 7, 8, 9, 10} <= {r for r, _ in styles[gold]}


def test_mounted_logo_uses_theme_colours(home):
    async def body(app, pilot):
        spans = {str(s.style).lower() for s in app.query_one("#now-logo", MaskLogo).render().spans}
        assert any("ebcb55" in s for s in spans) and any("00e5ff" in s for s in spans)
    _run(home, body)


def test_small_pane_shows_text_line_alone(home):
    async def body(app, pilot):
        logo = app.query_one("#now-logo", MaskLogo)
        assert _text(logo) == "Nothing needs you right now"
    _run(home, body, size=(60, 14))
