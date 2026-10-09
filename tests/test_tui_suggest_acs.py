"""Mounted-app tests for the suggest-ACs modal (`g`, T-113)."""
from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Checkbox
from textual.worker import WorkerFailed

from maestro import ops as ops_mod, snapshot as snap_mod, store
from maestro.tui import _SuggestAcsModal
from tui_support import _make_app


# --------------------------------------------------------------------------- #
# T-113: 'g' -> ops.suggest_acs (worker thread) -> _SuggestAcsModal -> ops.add_ac #
# --------------------------------------------------------------------------- #
# `ops_mod.suggest_acs` is mocked as the external boundary -- the same shape
# `_unreachable_ollama` uses for `ollama_mod.fetch_models` -- it's the bounded
# `claude -p` capture call from the TUI's point of view; the real subprocess/
# JSON-parsing logic underneath it has its own coverage in test_ops.py via the
# `run=` boundary.

def test_suggest_acs_action_notifies_when_no_ticket_selected(seeded_home):
    """The `key is None` guard the binding sweep exercises for every key."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = None
            notifications_before = len(app._notifications)
            await app.run_action("suggest_acs")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert len(app._notifications) > notifications_before
            assert app._exception is None

    asyncio.run(_inner())


def test_suggest_acs_action_refuses_when_ticket_already_has_acs(seeded_home, monkeypatch):
    """AC1: a ticket whose spec already has ACs refuses with a warning notify
    and writes nothing -- the worker (and any claude spawn) never even runs."""
    called = []
    monkeypatch.setattr(ops_mod, "suggest_acs",
                        lambda *a, **kw: called.append(1) or ["should never be used"])
    before = store.spec_path(seeded_home, "T-5").read_bytes()  # seeded with "- [ ] ok"

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            notifications_before = len(app._notifications)
            await app.run_action("suggest_acs")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert len(app._notifications) > notifications_before
            assert app._exception is None

    asyncio.run(_inner())
    assert not called
    assert store.spec_path(seeded_home, "T-5").read_bytes() == before


def _seed_ac_less(home, key="T-5"):
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\napproval_tier: 1\n\n## Intent\nready ticket\n")


def test_suggest_acs_modal_round_trip_writes_accepted_subset_only(seeded_home, monkeypatch):
    """AC2: on an AC-less ticket, the worker's suggestions land in the review
    modal; unchecking one and accepting writes exactly the checked subset to
    spec.md via `ops.add_ac`."""
    _seed_ac_less(seeded_home)
    monkeypatch.setattr(ops_mod, "suggest_acs",
                        lambda cfg, key, **kw: ["first suggestion", "second suggestion"])

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            notifications_before = len(app._notifications)
            await app.run_action("suggest_acs")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._exception is None
            modal = app.screen_stack[-1]
            assert isinstance(modal, _SuggestAcsModal)
            modal.query_one("#suggest-ac-1", Checkbox).value = False  # uncheck the 2nd
            await modal.run_action("submit")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1
            assert len(app._notifications) > notifications_before

    asyncio.run(_inner())
    after = store.spec_path(seeded_home, "T-5").read_text()
    assert snap_mod.parse_acs(after) == ["first suggestion"]


def test_suggest_acs_modal_cancel_writes_nothing(seeded_home, monkeypatch):
    """AC2: cancelling (Esc) writes nothing -- spec bytes on disk unchanged."""
    _seed_ac_less(seeded_home)
    before_bytes = store.spec_path(seeded_home, "T-5").read_bytes()
    monkeypatch.setattr(ops_mod, "suggest_acs", lambda cfg, key, **kw: ["a suggestion"])

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("suggest_acs")
            await app.workers.wait_for_complete()
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _SuggestAcsModal)
            await pilot.press("escape")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())
    assert store.spec_path(seeded_home, "T-5").read_bytes() == before_bytes


def test_suggest_acs_all_unchecked_accept_writes_nothing(seeded_home, monkeypatch):
    """Accepting with every suggestion unchecked behaves the same as cancel --
    an empty accepted list writes nothing (still a distinct code path from
    Esc, so worth covering on its own)."""
    _seed_ac_less(seeded_home)
    before_bytes = store.spec_path(seeded_home, "T-5").read_bytes()
    monkeypatch.setattr(ops_mod, "suggest_acs", lambda cfg, key, **kw: ["a suggestion"])

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("suggest_acs")
            await app.workers.wait_for_complete()
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _SuggestAcsModal)
            modal.query_one("#suggest-ac-0", Checkbox).value = False
            await modal.run_action("submit")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())
    assert store.spec_path(seeded_home, "T-5").read_bytes() == before_bytes


def test_suggest_acs_error_notify_on_spawn_failure(seeded_home, monkeypatch):
    """AC3: a failed/unavailable claude invocation surfaces as an error
    notify, leaves spec.md untouched, and does not crash the app -- no modal
    is ever pushed."""
    _seed_ac_less(seeded_home)
    before_bytes = store.spec_path(seeded_home, "T-5").read_bytes()

    def _boom(cfg, key, **kw):
        raise store.MaestroError(f"{key}: claude not found")
    monkeypatch.setattr(ops_mod, "suggest_acs", _boom)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            notifications_before = len(app._notifications)
            await app.run_action("suggest_acs")
            # `Worker.wait()` always re-raises a failed worker's own
            # exception into whoever awaits it, regardless of
            # `exit_on_error` -- that flag (set on this action's
            # `run_worker` call) only controls whether the App's own
            # exception handler *also* sees it, which is the actual AC3
            # "does not crash the app" guard, asserted below.
            with pytest.raises(WorkerFailed):
                await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1  # no modal pushed
            assert len(app._notifications) > notifications_before

    asyncio.run(_inner())
    assert store.spec_path(seeded_home, "T-5").read_bytes() == before_bytes
