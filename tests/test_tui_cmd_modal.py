"""Mounted-app tests for the `c` command modal (_CmdModal)."""
from __future__ import annotations

import asyncio

from textual.widgets import Input

from conftest import seed_ticket
from maestro import inbox, store
from maestro.tui import _CmdModal, _ConfirmModal
from tui_support import _make_app, _run_modal_test


def test_cmd_modal_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-2", "cmd", _CmdModal)  # degraded -> hint branch


def test_cmd_modal_shows_phase_commands(seeded_home):
    """_CmdModal renders phase-specific command rows for degraded, awaiting-human, and generic phases."""
    from maestro.tui import _PHASE_COMMANDS, _DEFAULT_COMMANDS

    cases = [
        ("degraded", _PHASE_COMMANDS["degraded"]),
        ("awaiting-human", _PHASE_COMMANDS["awaiting-human"]),
        ("ready", _DEFAULT_COMMANDS),
    ]

    def _check(phase, expected_commands):
        async def _inner():
            app = _make_app(seeded_home)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                modal = _CmdModal("T-x", phase)
                app.push_screen(modal, lambda _: None)
                await pilot.pause()
                rows = list(app.screen.query("Label.cmd-row"))
                assert len(rows) == len(expected_commands), (
                    f"phase={phase!r}: expected {len(expected_commands)} rows, got {len(rows)}"
                )
                row_texts = [str(lbl.content) for lbl in rows]
                for cmd, _desc in expected_commands:
                    assert any(cmd in t for t in row_texts), (
                        f"phase={phase!r}: cmd {cmd!r} not found in rows {row_texts}"
                    )
                assert app._exception is None
                await pilot.press("escape")
                await pilot.pause()
            assert app._exception is None

        asyncio.run(_inner())

    for phase, cmds in cases:
        _check(phase, cmds)


def _cmd_notifs(app):
    return [n.message for n in app._notifications]


def _submit_cmd(app, key, command):
    app._selected_key = key
    return app.run_action("cmd")


def test_cmd_refuses_qidless_answer_with_multiple_open_questions(home):
    seed_ticket(home, "T-1", "two questions", phase="awaiting-human",
                questions={"q1": "a?", "q2": "b?"})

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_cmd(app, "T-1", "approve")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _CmdModal)
            modal.query_one("#cmd-input", Input).value = "approve"
            modal._submit()
            await pilot.pause()
            msgs = [m for m in _cmd_notifs(app) if "T-1" in m and "2" in m]
            assert msgs
            assert not store.inbox_path(home, "T-1").exists() or not store.inbox_path(home, "T-1").read_text().strip()
            assert inbox.pending(home, "T-1") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_cmd_answer_single_open_question_still_queues(home):
    seed_ticket(home, "T-1", "one question", phase="awaiting-human",
                questions={"q1": "a?"})

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_cmd(app, "T-1", "approve")
            await pilot.pause()
            modal = app.screen_stack[-1]
            modal.query_one("#cmd-input", Input).value = "approve"
            modal._submit()
            await pilot.pause()
            pending = inbox.pending(home, "T-1")
            assert [c.get("command", c.get("cmd")) for c in pending] == ["approve"]
            assert not [m for m in _cmd_notifs(app) if "open questions" in m]
            assert app._exception is None

    asyncio.run(_inner())


def test_cmd_modal_has_no_requeue_row(home):
    from maestro.tui import _PHASE_COMMANDS, _DEFAULT_COMMANDS
    seed_ticket(home, "T-4", "ready ticket", phase="ready")
    for cmd, desc in _DEFAULT_COMMANDS + [r for rows in _PHASE_COMMANDS.values() for r in rows]:
        assert "requeue" not in cmd and "requeue" not in desc

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(_CmdModal("T-4", "ready"), lambda _: None)
            await pilot.pause()
            rows = [str(lbl.content) for lbl in app.screen.query("Label.cmd-row")]
            assert rows and not any("requeue" in r for r in rows)
            assert app._exception is None

    asyncio.run(_inner())


def test_cmd_modal_rows_match_real_routing():
    from maestro.tui import _PHASE_COMMANDS, _DEFAULT_COMMANDS
    all_rows = _DEFAULT_COMMANDS + [r for rows in _PHASE_COMMANDS.values() for r in rows]
    retries = [r for r in all_rows if r[0] == "retry"]
    assert retries and all(desc == "re-enter ready" for _c, desc in retries)
    assert not any("<qid>" in c or "<qid>" in d for c, d in all_rows)


def test_cmd_discard_not_refused_with_multiple_open_questions(home):
    seed_ticket(home, "T-1", "two questions", phase="awaiting-human",
                questions={"q1": "a?", "q2": "b?"})

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_cmd(app, "T-1", "discard")
            await pilot.pause()
            app.screen_stack[-1].dismiss(("discard", ""))
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ConfirmModal)
            modal.query_one("#confirm-input", Input).value = "T-1"
            await pilot.pause()
            modal.query_one("#confirm-ok").press()
            await pilot.pause()
            pending = inbox.pending(home, "T-1")
            assert [c.get("command", c.get("cmd")) for c in pending] == ["discard"]
            assert app._exception is None

    asyncio.run(_inner())
