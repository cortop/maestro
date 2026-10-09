"""Mounted-app tests for the phase-aware action menu (`m`, T-178)."""
from __future__ import annotations

import asyncio

from textual.widgets import Input, TextArea

from maestro import event_log, inbox, store
from maestro.tui import _ActionMenu, _AnswerModal, _CmdModal, _ConfirmModal, _InboxModal
from tui_support import _make_app


# --------------------------------------------------------------------------- #
# T-178: phase-aware action menu (m)                                          #
# --------------------------------------------------------------------------- #

async def _open_menu(app, pilot):
    await pilot.press("m")
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _choose(app, pilot, label):
    menu = app.screen_stack[-1]
    assert isinstance(menu, _ActionMenu)
    idx = next(i for i, r in enumerate(menu.rows) if r.label == label)
    menu.query_one("#menu-list").highlighted = idx
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


def _inbox_cmds(home, key):
    return store.read_jsonl(store.inbox_path(home, key))


def test_action_menu_degraded(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-2"
            await _open_menu(app, pilot)
            menu = app.screen_stack[-1]
            assert isinstance(menu, _ActionMenu)
            assert menu.title == "T-2 · degraded"
            rows = {r.label: r for r in menu.rows}
            assert rows["Retry"].enabled and rows["Discard"].enabled
            assert not rows["Answer"].enabled and rows["Answer"].reason == "no open questions"
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen_stack[-1], _ActionMenu)
            assert _inbox_cmds(seeded_home, "T-2") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_action_menu_approve_carries_qid(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        app._nudge_enabled = False
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await _open_menu(app, pilot)
            await _choose(app, pilot, "Approve")
            for _ in range(2):
                modal = app.screen_stack[-1]
                assert isinstance(modal, _AnswerModal)
                assert modal.query_one("#answer-input", TextArea).text == "approve"
                modal.action_submit()
                await pilot.pause()
            cmds = _inbox_cmds(seeded_home, "T-1")
            assert [c["command"] for c in cmds] == ["ans", "ans"]
            assert {c["args"]["qid"] for c in cmds} == {"q1", "q2"}
            assert app._exception is None

    asyncio.run(_inner())


def test_action_menu_dimmed_row_refuses(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        notes = []
        app.notify = lambda msg, **kw: notes.append(str(msg))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await _open_menu(app, pilot)
            await _choose(app, pilot, "Retry")
            assert not isinstance(app.screen_stack[-1], (_ActionMenu, _ConfirmModal))
            assert any("only for degraded tickets" in n for n in notes), notes
            for key in ("T-1", "T-2", "T-3", "T-4", "T-5"):
                assert _inbox_cmds(seeded_home, key) == []
            assert app._exception is None

    asyncio.run(_inner())


def test_action_menu_discard_uses_typed_confirm(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        app._nudge_enabled = False
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-2"
            await _open_menu(app, pilot)
            await _choose(app, pilot, "Discard")
            assert isinstance(app.screen_stack[-1], _ConfirmModal)
            await pilot.press("enter")
            await pilot.pause()
            assert inbox.pending(seeded_home, "T-2") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_action_menu_no_target_warns(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        notes = []
        app.notify = lambda msg, **kw: notes.append(str(msg))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = None
            before = len(app.screen_stack)
            await pilot.press("m")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(app.screen_stack) == before
            assert "Select a ticket first" in notes
            assert app._exception is None

    asyncio.run(_inner())


def test_action_menu_emits_only_human_commands(seeded_home, monkeypatch):
    """Select every enabled row on every seeded ticket; only ANSWER_COMMANDS | msg are
    ever queued and no phase/finalize/QA-verdict event is appended."""
    from maestro import ops

    monkeypatch.setenv("EDITOR", "true")
    forbidden = {"PhaseChanged", "Finalized", "AcQaVerdict"}
    keys = ["T-1", "T-2", "T-3", "T-4", "T-5"]
    before = {k: [e["type"] for e in event_log.read(seeded_home, k)] for k in keys}

    async def _row_labels(key):
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = key
            await _open_menu(app, pilot)
            return [r.label for r in app.screen_stack[-1].rows if r.enabled]

    async def _run_row(key, label):
        app = _make_app(seeded_home)
        app._nudge_enabled = False
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = key
            await _open_menu(app, pilot)
            await _choose(app, pilot, label)
            for _ in range(6):  # drain follow-up modals, submitting sample text or cancelling
                top = app.screen_stack[-1]
                if len(app.screen_stack) == 1:
                    break
                if isinstance(top, _AnswerModal):
                    top.query_one("#answer-input", TextArea).text = "sample"
                    top.action_submit()
                elif isinstance(top, _InboxModal):
                    top.dismiss("sample")
                elif isinstance(top, _CmdModal):
                    top.query_one("#cmd-input", Input).value = "retry"
                    top._submit()
                else:
                    await pilot.press("escape")
                await pilot.pause()
            assert app._exception is None, (key, label, app._exception)

    async def _all():
        for key in keys:
            for label in await _row_labels(key):
                await _run_row(key, label)

    asyncio.run(_all())
    allowed = set(ops.ANSWER_COMMANDS) | {"msg"}
    for key in keys:
        for cmd in _inbox_cmds(seeded_home, key):
            assert cmd["command"] in allowed, (key, cmd)
        after = [e["type"] for e in event_log.read(seeded_home, key)]
        new = after[len(before[key]):]
        assert not forbidden & set(new), (key, new)
