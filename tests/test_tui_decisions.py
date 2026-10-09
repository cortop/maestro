"""Mounted-app tests for the board-wide decision queue (DecisionsScreen, `W`)."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable

from conftest import seed_phase, seed_ticket
from maestro import inbox, snapshot as snap_mod
from maestro.cli import main as cli_main
from maestro.statemachine import Phase
from maestro.tui import DecisionsScreen, MaestroTUI, _ConfirmModal, _InboxModal
from tui_support import _bkey, _capture_toasts, _filter_idx, _make_app, _settle


# --------------------------------------------------------------------------- #
# T-164: board-wide decision queue (W)                                        #
# --------------------------------------------------------------------------- #

def _seed_dq(home):
    seed_phase(home, "T-1", Phase.TRIAGING)
    rc = cli_main(["--home", str(home), "ask", "T-1",
                   "--question", "Proceed with A?", "Yes, A", "qa1", "proceed",
                   "--question", "Which colour?", "blue", "qa2", "other"])
    assert rc == 0
    seed_phase(home, "T-6", Phase.TRIAGING)
    assert cli_main(["--home", str(home), "ask", "T-6", "--question", "Ship it?", "Yes", "q6",
                     "proceed"]) == 0
    seed_phase(home, "T-2", Phase.DEGRADED)


async def _open_decisions(app, pilot):
    await pilot.pause()
    await pilot.press("W")
    await pilot.pause()
    assert isinstance(app.screen, DecisionsScreen)
    await app.workers.wait_for_complete()
    await pilot.pause()
    return app.screen


def _dq_rows(screen):
    table = screen.query_one("#dq-table", DataTable)
    return [tuple(str(c) for c in table.get_row_at(i)) for i in range(table.row_count)]


def _ans(home, key):
    return [c for c in inbox.pending(home, key) if c["command"] == "ans"]


def test_decisions_screen_lists_open_questions(home):
    _seed_dq(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await _open_decisions(app, pilot)
            table = screen.query_one("#dq-table", DataTable)
            assert [str(c.label) for c in table.columns.values()] == [
                "Key", "Pos", "Kind", "Age", "Question", "Rec"]
            rows = _dq_rows(screen)
            assert [r[0] for r in rows] == ["T-1", "T-1", "T-2", "T-6"]
            assert [r[1] for r in rows[:2]] == ["1/2", "2/2"]
            assert [r[2] for r in rows] == ["proceed", "other", "degraded", "proceed"]
            assert rows[0][5] == "Yes, A" and rows[0][3] != "…"
            assert app._exception is None

    asyncio.run(_inner())


def test_decisions_accept_recommendation_once(home):
    _seed_dq(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await _open_decisions(app, pilot)
            table = screen.query_one("#dq-table", DataTable)
            await pilot.press("y")
            await pilot.pause()
            snap = snap_mod.load(home, "T-1")
            ans = _ans(home, "T-1")
            assert len(ans) == 1
            qid = ans[0]["args"]["qid"]
            assert ans[0]["args"]["text"] == snap.question_recommends[qid]
            assert ans[0]["args"]["accepted_recommendation"] is True
            assert table.cursor_row == 1
            await pilot.press("up", "y")
            await pilot.pause()
            assert len(_ans(home, "T-1")) == 1
            assert _dq_rows(screen)[0][5] == "queued"
            assert app._exception is None

    asyncio.run(_inner())


def test_decisions_bulk_accept_proceed_only(home):
    _seed_dq(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await _open_decisions(app, pilot)
            await pilot.press("space", "down", "space", "Y")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, _ConfirmModal)
            assert "partial: will spawn" in modal._message and "T-1" in modal._message
            await pilot.press("enter")  # default No
            await pilot.pause()
            assert screen is app.screen and _ans(home, "T-1") == []
            await pilot.press("Y")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            ans = _ans(home, "T-1")
            assert [a["args"]["qid"] for a in ans] == ["qa1"]
            assert app._exception is None

    asyncio.run(_inner())


def test_decisions_retry_discard_degraded(home):
    _seed_dq(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await _open_decisions(app, pilot)
            await pilot.press("down", "down")  # T-2
            assert screen.current_key() == "T-2"
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, _ConfirmModal)
            await pilot.press("enter")
            await pilot.pause()
            assert inbox.pending(home, "T-2") == []
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press(*"T-2")
            await pilot.press("enter")
            await pilot.pause()
            assert [c["command"] for c in inbox.pending(home, "T-2")] == ["discard"]
            assert app._exception is None
        app = _make_app(home)
        home2 = home
        # retry on a fresh queue (the discard above is still pending -> use T-2 again after ack)
        inbox.ack(home2, "T-2")
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await _open_decisions(app, pilot)
            await pilot.press("down", "down", "r")
            await pilot.pause()
            assert [c["command"] for c in inbox.pending(home, "T-2")] == ["retry"]
            await pilot.press("up", "r")  # non-degraded row: warn, write nothing
            await pilot.pause()
            assert inbox.pending(home, "T-1") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_decisions_close_nudges_once(home):
    _seed_dq(home)
    seed_phase(home, "T-5", Phase.READY)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            toasts = _capture_toasts(app)
            await _open_decisions(app, pilot)
            await pilot.press("escape")  # nothing written -> nothing nudged
            await _settle(app, pilot)
            assert app.dry.spawned == [] and not any("answered" in t for t in toasts)
            await pilot.press("W")
            await pilot.pause()
            await pilot.press("y", "down", "down", "down", "y")  # T-1/qa1 and T-6
            await pilot.pause()
            assert app.dry.spawned == []
            await pilot.press("escape")
            await _settle(app, pilot)
            assert sorted(set(s[0] for s in app.dry.spawned)) <= ["T-1", "T-6"]
            assert len([s for s in app.dry.spawned if s[0] == "T-1"]) <= 1
            assert not any(s[0] == "T-5" for s in app.dry.spawned)
            assert len([t for t in toasts if "answered" in t]) == 1
            assert app._exception is None

    asyncio.run(_inner())


def test_answer_key_opens_decision_queue(home):
    _seed_dq(home)
    seed_ticket(home, "T-3", "implementing", phase="implementing")

    async def _inner():
        from textual.widgets import Input as _Input
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.move_cursor(row=[str(k.value) for k in table.rows].index("T-3"))
            await pilot.pause()
            assert app._selected_key == "T-3"
            await pilot.press("a")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DecisionsScreen)
            assert _ans(home, "T-3") == []
            await pilot.press("down", "down")  # T-2
            await pilot.press("i")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, _InboxModal)
            modal.query_one("#inbox-input", _Input).value = "hello"
            await pilot.press("enter")
            await pilot.pause()
            assert [c["args"]["text"] for c in inbox.pending(home, "T-2")] == ["hello"]
            assert inbox.pending(home, "T-3") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_decisions_footer_and_resize_bindings():
    from textual.binding import Binding
    visible = {_bkey(b) for b in MaestroTUI.BINDINGS if not isinstance(b, Binding) or b.show}
    assert "W" in visible and "[" not in visible and "]" not in visible
    assert len(visible) <= 10
