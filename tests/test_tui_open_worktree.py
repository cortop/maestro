"""Mounted-app tests for the `G` open-worktree action (T-190)."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable

from conftest import seed_ticket
from maestro import store
from maestro.tui import ReviewScreen, worktree_open
from tui_support import _make_app


def _env(monkeypatch, *, cmux=False, term=None):
    monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    if cmux:
        monkeypatch.setenv("CMUX_SOCKET_PATH", "/tmp/cmux.sock")
    if term:
        monkeypatch.setenv("TERM_PROGRAM", term)
    monkeypatch.setattr(worktree_open.shutil, "which",
                        lambda name: "/usr/bin/cmux" if cmux and name == "cmux" else None)
    calls: list[list[str]] = []
    monkeypatch.setattr(worktree_open, "runner", calls.append)
    return calls


def _seed(home, with_worktree=True):
    seed_ticket(home, "T-1", "open me", phase="awaiting-ci")
    wt = store.worktree_path(home, "T-1")
    if with_worktree:
        wt.mkdir(parents=True, exist_ok=True)
    return wt


def _press_g(home, screen_review=False):
    notes: list[tuple[str, str]] = []

    async def _inner():
        app = _make_app(home)
        orig = app.notify
        app.notify = lambda msg, *a, **kw: (notes.append((str(msg), kw.get("severity", "information"))),
                                            orig(msg, *a, **kw))[1]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            if screen_review:
                await pilot.press("R")
                await pilot.pause()
                assert isinstance(app.screen, ReviewScreen)
                app.screen.query_one("#review-table", DataTable).focus()
            await pilot.press("G")
            await pilot.pause()
            assert app._exception is None
    asyncio.run(_inner())
    return notes


def test_cmux_opens_workspace(home, monkeypatch):
    calls = _env(monkeypatch, cmux=True, term="Apple_Terminal")
    wt = _seed(home)
    _press_g(home)
    assert calls == [["cmux", str(wt)]]


def test_apple_terminal_opens_tab(home, monkeypatch):
    calls = _env(monkeypatch, term="Apple_Terminal")
    wt = _seed(home)
    _press_g(home)
    assert calls == [["open", "-a", "Terminal", str(wt)]]


def test_fallback_notifies_path(home, monkeypatch):
    calls = _env(monkeypatch, term="xterm")
    wt = _seed(home)
    notes = _press_g(home)
    assert calls == []
    assert any(str(wt) in m for m, _ in notes)


def test_no_worktree_warns(home, monkeypatch):
    calls = _env(monkeypatch, cmux=True)
    _seed(home, with_worktree=False)
    notes = _press_g(home)
    assert calls == []
    assert ("No worktree for T-1", "warning") in notes


def test_review_screen_g_opens_worktree(home, monkeypatch):
    calls = _env(monkeypatch, cmux=True)
    wt = _seed(home)
    _press_g(home, screen_review=True)
    assert calls == [["cmux", str(wt)]]
