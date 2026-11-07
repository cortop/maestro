"""Event-log pane renders payloads holding Rich-markup-like text literally."""
from __future__ import annotations

import asyncio

from rich.text import Text
from textual.widgets import DataTable, RichLog

from conftest import seed_ticket
from maestro.tui.events import render_event
from tui_support import _make_app

_HOSTILE = "render tool_result is_error as [red]err[/x] and [/] in the [b]pane"


def test_render_event_escapes_payload_markup():
    """A stray closing tag in a payload parses as plain text, not a MarkupError."""
    ev = {"seq": 3, "ts": "2026-11-07T03:18:24+00:00", "type": "QuestionAsked",
          "actor": "reconciler[/a]", "payload": {"qid": "q", "text": _HOSTILE}}
    plain = Text.from_markup(render_event(ev)).plain
    assert "[red]err[/x]" in plain
    assert "reconciler[/a]" in plain


def test_render_event_escapes_impl_step_and_unknown_type():
    """Impl-step summary/kind and an unrecognised type name are escaped too."""
    step = {"seq": 1, "ts": "t", "type": "ImplStepRecorded", "actor": "r",
            "payload": {"kind": "odd[/k]", "summary": "fix [/x] tag"}}
    assert "fix [/x] tag" in Text.from_markup(render_event(step)).plain
    unknown = {"seq": 2, "ts": "t", "type": "Weird[/t]", "actor": "r", "payload": {}}
    assert "Weird[/t]" in Text.from_markup(render_event(unknown)).plain


def test_highlighting_ticket_with_markup_question_does_not_crash(home):
    """Moving the board cursor onto a ticket whose question holds ``[/x]`` renders
    the #events pane instead of crashing the app."""
    seed_ticket(home, "T-1", "plain ticket", phase="ready")
    seed_ticket(home, "T-2", "markup question", phase="awaiting-human",
                questions={"q1": _HOSTILE})

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.focus()
            row = next(i for i in range(table.row_count)
                       if table.coordinate_to_cell_key((i, 0)).row_key.value == "T-2")
            table.move_cursor(row=row)
            await pilot.pause()
            assert app._exception is None
            log = app.query_one("#events", RichLog)
            assert any("[red]err[/x]" in strip.text for strip in log.lines)

    asyncio.run(_inner())
