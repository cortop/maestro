"""Contextual footer and palette ticket-action group on the real MaestroTUI (T-248)."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable
from textual.widgets._footer import FooterKey

from conftest import seed_ticket
from tui_support import _make_app

REC = "Approve: proceed"


def _run(home, body):
    async def _inner():
        app = _make_app(home)
        app._nudge_enabled = False
        async with app.run_test(size=(140, 40)) as pilot:
            await _settle(pilot)
            await body(app, pilot)
            assert app._exception is None
    asyncio.run(_inner())


async def _settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause(0.2)


def _footer(app) -> list[str]:
    return [k.action for k in app.screen.query(FooterKey)]


def _labels(app) -> list[str]:
    from textual.command import CommandList
    lst = app.screen.query_one(CommandList)
    return [str(lst.get_option_at_index(i).prompt) for i in range(lst.option_count)]


def _seed(home):
    seed_ticket(home, "Q-1", "needs an answer", phase="awaiting-human",
                questions={"q1": f"Pick it up?\n   Recommended: {REC}"})
    seed_ticket(home, "D-1", "broken", phase="degraded")


def test_footer_on_now_question_row(home):
    _seed(home)

    async def body(app, pilot):
        await pilot.press("down")  # the degraded row sorts first
        await _settle(pilot)
        acts = _footer(app)
        assert {"now_accept", "answer", "show_spec", "view_logs", "hold",
                "show_help_panel", "command_palette"} <= set(acts), acts
        assert "retry" not in acts and "create" not in acts and "cycle_filter" not in acts
    _run(home, body)


def test_footer_follows_selected_row_and_tab(home):
    _seed(home)

    async def body(app, pilot):
        await _settle(pilot)
        acts = _footer(app)  # first Now row is the degraded ticket
        assert "retry" in acts and "discard" in acts and "answer" not in acts, acts
        await pilot.press("down")
        await _settle(pilot)
        assert "answer" in _footer(app) and "retry" not in _footer(app)
        await pilot.press("2")
        await _settle(pilot)
        app.query_one("#tickets", DataTable).move_cursor(row=0)
        await _settle(pilot)
        acts = _footer(app)
        assert {"show_spec", "hold"} <= set(acts) and "create" not in acts, acts
        await pilot.press("3")
        await _settle(pilot)
        acts = _footer(app)
        assert "hold" not in acts and "show_spec" not in acts and "fleet_panel" in acts, acts
        assert "show_help_panel" in acts and "command_palette" in acts
    _run(home, body)


def test_palette_leads_with_selected_ticket_actions(home):
    _seed(home)

    async def body(app, pilot):
        await pilot.press("ctrl+p")
        for _ in range(100):
            await pilot.pause(0.05)
            if any("D-1:" in p for p in _labels(app)):
                break
        labels = _labels(app)
        first = [i for i, l in enumerate(labels) if l.startswith(("Spec (", "Hold (", "Logs (", "Answer ("))]
        other = labels.index(next(l for l in labels if l.startswith("Refresh (")))
        assert len(first) == 4 and max(first) < other, labels
        from textual.command import CommandList
        clist = app.screen.query_one(CommandList)
        prompts = [str(clist.get_option_at_index(i).prompt) for i in range(clist.option_count)]
        assert any("D-1:" in p for p in prompts)  # one-line help names the selected ticket
    _run(home, body)


def test_existing_keys_still_work_with_filtered_footer(home):
    _seed(home)

    async def body(app, pilot):
        from maestro.tui import SpecScreen
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, SpecScreen)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("f")  # hidden from the footer here, still bound
        await pilot.pause()
        assert app._filter_idx != 0
    _run(home, body)
