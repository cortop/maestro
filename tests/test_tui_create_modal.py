"""Mounted-app tests for the create (`n`) and Linear import modals."""
from __future__ import annotations

import asyncio
import json

from textual.widgets import Input, Select, TextArea

from conftest import seed_ticket
from maestro import store
from maestro.tui import _CreateModal, _ImportLinearModal
from tui_support import _make_app, _run_modal_test


def test_create_modal_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, None, "create", _CreateModal)


def test_import_linear_modal_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, None, "import_linear", _ImportLinearModal)


def test_import_linear_flow_mints_ticket(seeded_home, monkeypatch):
    """T-103: type an identifier into the real modal, submit it through the
    real app, and assert the ticket actually got minted -- the only thing
    faked is the Linear transport (`LinearTracker._transport_or_build`),
    exactly like the non-TUI adapter tests fake it."""
    from maestro.providers import linear as linear_mod

    class FakeLinearTransport:
        def search_issues(self, filter):
            return []

        def get_issue(self, identifier):
            return {"identifier": "ENG-42", "title": "Fix the thing",
                     "description": "Do the fix", "state": {"name": "Todo"}}

        def get_comments(self, identifier):
            return []

    monkeypatch.setattr(linear_mod.LinearTracker, "_transport_or_build",
                        lambda self: FakeLinearTransport())

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("import_linear")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _ImportLinearModal)
            inp = app.screen.query_one("#import-linear-input", Input)
            inp.value = "ENG-42"
            inp.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1, "modal should have dismissed"

    asyncio.run(_inner())
    assert store.spec_path(seeded_home, "LINEAR-ENG-42").exists()


def test_import_linear_malformed_input_notifies_without_crash(seeded_home):
    """A garbage identifier surfaces as a notify(), not a crash -- proves the
    `store.MaestroError` from `parse_identifier` is caught in the app, not
    left to escape as an unhandled exception."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("import_linear")
            await pilot.pause()
            inp = app.screen.query_one("#import-linear-input", Input)
            inp.value = "not a linear thing"
            inp.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())


def test_import_linear_transport_failure_notifies_without_crash(seeded_home, monkeypatch):
    """T-111: a transport-level failure (network down, bad/missing API key,
    a Linear GraphQL error) must surface as an error notify(), not take the
    whole app down -- `ops.import_linear` now normalizes these into
    `store.MaestroError` at the ops boundary, so the app's existing
    `except store.MaestroError` catches this exactly like a malformed
    identifier already does above."""
    import urllib.error

    from maestro.providers import linear as linear_mod

    class FailingLinearTransport:
        def search_issues(self, filter):
            return []

        def get_issue(self, identifier):
            raise urllib.error.URLError("network is unreachable")

        def get_comments(self, identifier):
            return []

    monkeypatch.setattr(linear_mod.LinearTracker, "_transport_or_build",
                        lambda self: FailingLinearTransport())

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("import_linear")
            await pilot.pause()
            inp = app.screen.query_one("#import-linear-input", Input)
            inp.value = "ENG-42"
            inp.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    assert not store.spec_path(seeded_home, "LINEAR-ENG-42").exists()


def test_create_modal_intent_is_textarea_and_accepts_multiline(seeded_home):
    """Intent field must be a TextArea (not an Input) and must accept newlines."""

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("create")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _CreateModal), "create modal did not open"
            modal = app.screen_stack[-1]
            # Intent widget must be a TextArea, not a single-line Input
            intent_widget = modal.query_one("#create-intent")
            assert isinstance(intent_widget, TextArea), (
                f"Intent field should be TextArea, got {type(intent_widget).__name__}"
            )
            # Focus and type a two-line intent
            intent_widget.focus()
            await pilot.pause()
            await pilot.press("h", "e", "l", "l", "o")
            await pilot.press("enter")  # newline inside TextArea
            await pilot.press("w", "o", "r", "l", "d")
            await pilot.pause()
            text = intent_widget.text
            assert "\n" in text, f"TextArea should contain newline, got: {text!r}"
            assert "hello" in text and "world" in text
            assert app._exception is None
            await pilot.press("escape")

    asyncio.run(_inner())


def test_create_modal_submits_prefix_to_inbox(seeded_home):
    """Fill in title + existing prefix via Select, submit, verify _new inbox entry."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("create")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _CreateModal)
            modal = app.screen_stack[-1]
            # Fill in title
            modal.query_one("#create-title", Input).value = "My new feature"
            # Select the "T" prefix (seeded_home has T-1..T-5)
            modal.query_one("#create-prefix", Select).value = "T"
            await pilot.pause()
            # Enter on last visible Input moves focus to TextArea; use Ctrl+Enter to submit
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1  # modal dismissed

        # Verify the inbox entry has the right prefix
        new_path = store.new_inbox_path(seeded_home)
        entries = [json.loads(line) for line in new_path.read_text().splitlines() if line.strip()]
        assert entries, "no entry written to _new inbox"
        last = entries[-1]
        assert last["title"] == "My new feature"
        assert last["prefix"] == "T"

    asyncio.run(_inner())


def test_create_modal_empty_intent_omitted_from_args(seeded_home):
    """Leaving intent blank must omit the "intent" key from args entirely
    (not write an explicit null) — matches the CLI's create-args convention."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("create")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _CreateModal)
            modal = app.screen_stack[-1]
            modal.query_one("#create-title", Input).value = "No intent here"
            modal.query_one("#create-prefix", Select).value = "T"
            await pilot.pause()
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1  # modal dismissed

        new_path = store.new_inbox_path(seeded_home)
        entries = [json.loads(line) for line in new_path.read_text().splitlines() if line.strip()]
        assert entries, "no entry written to _new inbox"
        last = entries[-1]
        assert last["title"] == "No intent here"
        assert "intent" not in last.get("args", {}), (
            f"intent should be omitted when empty, got args: {last.get('args')}"
        )

    asyncio.run(_inner())


def test_create_modal_prefix_select_has_options(seeded_home):
    """_CreateModal shows existing prefix T (from seeded_home) + (new) in the Select;
    the new-prefix Input is hidden when an existing prefix is pre-selected."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("create")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _CreateModal)
            modal = app.screen_stack[-1]
            sel = modal.query_one("#create-prefix", Select)
            option_values = {v for _, v in sel._options}
            assert "T" in option_values, f"expected T in options: {option_values}"
            assert "(new)" in option_values
            new_inp = modal.query_one("#create-prefix-new", Input)
            assert not new_inp.display, "new-prefix input should start hidden"
            assert app._exception is None
    asyncio.run(_inner())


def test_create_modal_defaults_prefix_to_m_when_present(home):
    """When M is among existing prefixes, the Select should pre-select it."""
    seed_ticket(home, "T-1", "ticket", phase="ready")
    seed_ticket(home, "M-1", "ticket", phase="ready")
    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("create")
            await pilot.pause()
            modal = app.screen_stack[-1]
            sel = modal.query_one("#create-prefix", Select)
            assert sel.value == "M"
            assert app._exception is None
    asyncio.run(_inner())


def test_create_modal_falls_back_to_first_prefix_when_m_absent(seeded_home):
    """seeded_home only has prefix T (no M) — Select should fall back to the
    first-option behavior, unchanged from before this default was added."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("create")
            await pilot.pause()
            modal = app.screen_stack[-1]
            sel = modal.query_one("#create-prefix", Select)
            assert sel.value == "T"
            assert app._exception is None
    asyncio.run(_inner())


def test_create_modal_new_prefix_reveals_input(seeded_home):
    """Selecting (new) in the prefix Select reveals the new-prefix Input."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("create")
            await pilot.pause()
            modal = app.screen_stack[-1]
            sel = modal.query_one("#create-prefix", Select)
            new_inp = modal.query_one("#create-prefix-new", Input)
            assert not new_inp.display, "new-prefix input starts hidden"
            # drive on_select_changed directly — setting .value programmatically
            # does not emit Select.Changed in Textual 8
            modal.on_select_changed(Select.Changed(select=sel, value="(new)"))
            await pilot.pause()
            assert new_inp.display, "new-prefix input should be visible after (new)"
            assert app._exception is None
    asyncio.run(_inner())


def test_create_modal_has_kind_model_effort_fields(seeded_home):
    """_CreateModal exposes kind Select, model Input, and effort Input widgets."""
    from textual.widgets import Select

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("create")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _CreateModal)
            modal.query_one("#create-kind", Select)
            modal.query_one("#create-model", Input)
            modal.query_one("#create-effort", Input)
            assert app._exception is None
            await pilot.press("escape")

    asyncio.run(_inner())


def test_create_modal_research_kind_fills_defaults(seeded_home):
    """Selecting research kind auto-fills model=opus and effort=high."""
    from textual.widgets import Select

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("create")
            await pilot.pause()
            modal = app.screen_stack[-1]
            kind_sel = modal.query_one("#create-kind", Select)
            model_inp = modal.query_one("#create-model", Input)
            effort_inp = modal.query_one("#create-effort", Input)
            modal.on_select_changed(Select.Changed(select=kind_sel, value="research"))
            await pilot.pause()
            assert model_inp.value == "opus", f"expected opus, got {model_inp.value!r}"
            assert effort_inp.value == "high", f"expected high, got {effort_inp.value!r}"
            assert app._exception is None
            await pilot.press("escape")

    asyncio.run(_inner())


def test_create_modal_submits_kind_model_effort(seeded_home):
    """Submit with kind=research writes kind/model/effort to the _new inbox."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("create")
            await pilot.pause()
            modal = app.screen_stack[-1]
            from textual.widgets import Select as TSelect
            modal.query_one("#create-title", Input).value = "Research feature"
            modal.query_one("#create-prefix", TSelect).value = "T"
            kind_sel = modal.query_one("#create-kind", TSelect)
            kind_sel.value = "research"
            modal.on_select_changed(TSelect.Changed(select=kind_sel, value="research"))
            await pilot.pause()
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

        new_path = store.new_inbox_path(seeded_home)
        entries = [json.loads(line) for line in new_path.read_text().splitlines() if line.strip()]
        last = entries[-1]
        assert last["title"] == "Research feature"
        assert last.get("args", {}).get("kind") == "research"
        assert last.get("args", {}).get("model") == "opus"
        assert last.get("args", {}).get("effort") == "high"

    asyncio.run(_inner())
