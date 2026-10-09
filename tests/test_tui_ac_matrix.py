"""Mounted-app tests for the AC evidence matrix (AcScreen, `v`, T-167)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import DataTable, Input, Static

from conftest import seed_ticket
from maestro import event_log, inbox, ops as ops_mod, snapshot as snap_mod, store
from maestro.tui import AcScreen, _AcEvidenceModal, SpecScreen, _AddAcModal, _InboxModal
from tui_support import _make_app


# --------------------------------------------------------------------------- #
# T-167: AC evidence matrix (AcScreen, `v`)                                    #
# --------------------------------------------------------------------------- #

def _seed_ac_ticket(home, key="T-9", *, worktree=False):
    from maestro.config import Config
    store.atomic_write(
        store.spec_path(home, key),
        f"# {key}: ac matrix\npriority: 2\n\n## Intent\nx\n\n## Acceptance criteria\n"
        "- [ ] first shows [bold] literally\n- [ ] second fails qa\n"
        "- [ ] third (test: tests/test_x.py::test_x)\n")
    event_log.append(home, key, "TicketCreated", {"title": "ac matrix", "source": "test"}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": "qa", "reason": ""}, actor="r")
    snap_mod.rebuild(home, key)
    cfg = Config(home=home)
    acs = snap_mod.parse_acs(store.spec_path(home, key).read_text())
    ops_mod.verify_ac(cfg, key, 1, {"what": "ran", "where": "t.py", "result": "ok"})
    ops_mod.record_qa_verdict(cfg, key, 2, "fail", "second is broken")
    event_log.append(home, key, "AcCheckCaptured", {
        "tree_key": "1234567abc:dead", "ac_hash": snap_mod.ac_hash(acs[2]), "ac_index": 3,
        "ac_text": acs[2], "kind": "test", "command": "pytest tests/test_x.py", "exit_code": 1,
        "passed": False, "failure_excerpt": "AssertionError: kaboom"}, actor="d")
    snap_mod.rebuild(home, key)
    if worktree:
        store.worktree_path(home, key).mkdir(parents=True)


def _table_text(app):
    t = app.screen.query_one("#ac-table", DataTable)
    return [[str(c) for c in t.get_row_at(i)] for i in range(t.row_count)]


def test_ac_matrix_rows_and_header(home):
    _seed_ac_ticket(home)
    seed_ticket(home, "T-1", "other")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, AcScreen)
            assert str(app.screen.query_one("#ac-summary", Static).render()) == \
                "QA spec 0/3 pass · 1 fail · 2 pending"
            rows = _table_text(app)
            assert [r[0] for r in rows] == ["1", "2", "3"]
            assert rows[0][1] == "first shows [bold] literally"
            assert "fail" in rows[1][3]
            assert app._exception is None

    asyncio.run(_inner())


def test_ac_matrix_targets_visible_ticket(home):
    _seed_ac_ticket(home)
    seed_ticket(home, "T-1", "other")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            app.push_screen(SpecScreen(home, "T-9"))
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, AcScreen)
            assert app.screen._key == "T-9"
            assert app._exception is None

    asyncio.run(_inner())


def test_ac_matrix_evidence_modal(home):
    _seed_ac_ticket(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            assert isinstance(app.screen, _AcEvidenceModal)
            body = " ".join(str(w.render()) for w in app.screen.query(Static))
            assert "AssertionError: kaboom" in body
            assert "pytest tests/test_x.py" in body
            assert app._exception is None

    asyncio.run(_inner())


def test_ac_matrix_message_writes_no_evidence(home):
    _seed_ac_ticket(home)
    before = len(event_log.read(home, "T-9"))

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("down", "m")
            await pilot.pause()
            assert isinstance(app.screen, _InboxModal)
            inp = app.screen.query_one("#inbox-input", Input)
            assert inp.value == "re AC2: "
            inp.value = "re AC2: please look"
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app._exception is None

    asyncio.run(_inner())
    msgs = [e for e in inbox.pending(home, "T-9") if e["command"] == "msg"]
    assert len(msgs) == 1 and msgs[0]["args"]["text"] == "re AC2: please look"
    assert len(event_log.read(home, "T-9")) == before


def test_ac_matrix_add_ac_and_editor_line(home, monkeypatch):
    from maestro.tui.screens import editor_argv
    monkeypatch.setenv("VISUAL", "vim")
    assert editor_argv(Path("/p/spec.md"), line=7) == ["vim", "+7", "/p/spec.md"]
    assert editor_argv(Path("/p/spec.md")) == ["vim", "/p/spec.md"]
    _seed_ac_ticket(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            assert len(_table_text(app)) == 3
            await pilot.press("A")
            await pilot.pause()
            assert isinstance(app.screen, _AddAcModal)
            app.screen.query_one("#add-ac-input", Input).value = "fourth one"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, AcScreen)
            assert len(_table_text(app)) == 4
            assert app._exception is None

    asyncio.run(_inner())


def test_ac_matrix_edit_passes_ac_line_to_editor(home, monkeypatch):
    import maestro.tui.screens as screens_mod
    _seed_ac_ticket(home)
    seen = {}
    monkeypatch.setattr(screens_mod, "edit_in_editor",
                        lambda app, path, line=None: seen.update(line=line))

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("down", "E")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    lines = store.spec_path(home, "T-9").read_text().splitlines()
    assert lines[seen["line"] - 1].startswith("- [ ] second")


def test_ac_matrix_no_worktree_is_read_only(home):
    _seed_ac_ticket(home)
    before = len(event_log.read(home, "T-9"))

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert "no worktree" in _table_text(app)[2][5]
            assert app._exception is None

    asyncio.run(_inner())
    assert not (home / "scratch").exists()
    assert len(event_log.read(home, "T-9")) == before
