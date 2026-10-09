"""Mounted-app tests for the review cockpit (`R`, T-177)."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable

from conftest import add_worktree, git, make_origin_and_repo, seed_ticket
from maestro.tui.modals import _TextViewModal
from maestro import event_log, inbox, snapshot as snap_mod, store
from maestro.tui import ReviewScreen, DetailScreen, MaestroTUI
from tui_support import _bkey, _filter_idx, _make_app


# --------------------------------------------------------------------------- #
# T-177: review cockpit (R)                                                    #
# --------------------------------------------------------------------------- #

def _seed_review(home):
    """T-4 (awaiting-ci, PR 16): a failing CiObserved, one unreplied COMMENTED review,
    one APPROVED review, one replied inline comment."""
    event_log.append(home, "T-4", "CiObserved",
                     {"state": "failing", "failing_checks": ["lint"],
                      "failure_excerpt": "BOOM-EXCERPT"}, actor="d")
    event_log.append(home, "T-4", "ReviewFeedbackReceived",
                     {"comment_id": "c1", "state": "COMMENTED", "body": "please rename x",
                      "author": "bob"}, actor="d")
    event_log.append(home, "T-4", "ReviewFeedbackReceived",
                     {"comment_id": "c2", "state": "APPROVED", "body": "lgtm", "author": "amy"},
                     actor="d")
    event_log.append(home, "T-4", "ReviewFeedbackReceived",
                     {"comment_id": "inline-9", "state": "COMMENTED", "body": "nit here",
                      "author": "bob", "path": "a.py", "line": 3}, actor="d")
    event_log.append(home, "T-4", "ReviewReplyPosted",
                     {"comment_id": "inline-9", "tree_sha": "abc", "kind": "inline",
                      "body": "REPLY-BODY fixed"}, actor="r")
    snap_mod.rebuild(home, "T-4")


def _cells(screen, key):
    table = screen.query_one("#review-table", DataTable)
    return [str(c) for c in table.get_row(key)]


def test_review_cockpit_lists_reviewable_tickets(seeded_home):
    _seed_review(seeded_home)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            assert isinstance(app.screen, ReviewScreen)
            table = app.screen.query_one("#review-table", DataTable)
            assert [r.value for r in table.rows] == ["T-4"]
            cells = _cells(app.screen, "T-4")
            assert cells[1] == "16"
            assert cells[2] == "failing (1)"
            assert cells[3] == "1"
            assert cells[4] == "0/1"
            assert cells[6] == ""
            assert app._exception is None

    asyncio.run(_inner())


def test_review_cockpit_copies_merge_command(seeded_home):
    _seed_review(seeded_home)
    log = store.events_path(seeded_home, "T-4")
    before = log.read_bytes()

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            assert app.clipboard == "gh pr merge 16 --repo cortop/maestro"
            assert app._exception is None

    asyncio.run(_inner())
    assert log.read_bytes() == before


def test_review_cockpit_threads_and_ci_timeline(seeded_home):
    _seed_review(seeded_home)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert isinstance(app.screen, _TextViewModal)
            text = app.screen.text
            assert "nit here" in text and "REPLY-BODY fixed" in text
            assert "a.py:3" in text and "please rename x" in text
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()
            assert isinstance(app.screen, _TextViewModal)
            assert "BOOM-EXCERPT" in app.screen.text and "lint" in app.screen.text
            assert app._exception is None

    asyncio.run(_inner())


def test_review_cockpit_diff(seeded_home, tmp_path_factory):
    async def _inner(app, expect):
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            await pilot.press("d")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            if expect is None:
                assert isinstance(app.screen, ReviewScreen)
            else:
                assert isinstance(app.screen, _TextViewModal)
                assert expect in app.screen.text
            assert app._exception is None

    # no worktree: a warning, no crash
    asyncio.run(_inner(_make_app(seeded_home), None))

    # real worktree with a committed change
    origin, repo = make_origin_and_repo(tmp_path_factory.mktemp("repo"))
    wt = add_worktree(repo, seeded_home, "T-4", "maestro/T-4")
    (wt / "feature.txt").write_text("brand-new-line\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "change", cwd=wt)
    git("fetch", "-q", "origin", cwd=wt)
    asyncio.run(_inner(_make_app(seeded_home), "brand-new-line"))


def test_review_cockpit_open_detail_and_message(seeded_home, monkeypatch):
    seed_ticket(seeded_home, "T-6", "second reviewable", phase="in-review", pr=17)
    opened = []
    monkeypatch.setattr(MaestroTUI, "open_url", lambda self, url, **kw: opened.append(url))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"  # board cursor elsewhere
            await pilot.press("R")
            await pilot.pause()
            assert app.screen._key == "T-4"
            await pilot.press("o")
            await pilot.pause()
            assert opened == ["https://github.com/cortop/maestro/pull/16"]
            await pilot.press("down")
            await pilot.pause()
            assert app.screen._key == "T-6"
            await pilot.press("i")
            await pilot.pause()
            await pilot.press(*"fix it")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ReviewScreen)
            pend = inbox.pending(seeded_home, "T-6")
            assert [c["args"]["text"] for c in pend] == ["fix it"]
            assert not inbox.pending(seeded_home, "T-3")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen) and app.screen._key == "T-6"
            assert app._exception is None

    asyncio.run(_inner())


def test_review_filter_preset(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("review")
            app._populate()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            assert [r.value for r in table.rows] == ["T-4"]
            assert app._exception is None

    asyncio.run(_inner())
    r = next(b for b in MaestroTUI.BINDINGS if _bkey(b) == "R")
    assert r.show
