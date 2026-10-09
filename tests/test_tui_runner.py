"""Mounted-app tests for the runner modal (_RunnerModal), including the pi runner kind."""
from __future__ import annotations

import asyncio
import subprocess
import sys

from textual.widgets import Input, Select, Static

from maestro import claims, store
from maestro.tui import _RunnerModal
from tui_support import _make_app, _run_modal_test


# --------------------------------------------------------------------------- #
# UX-2: runner row + modal ('o' -- show and change the runner)                #
# --------------------------------------------------------------------------- #

def _unreachable_ollama(monkeypatch):
    """Deterministic "daemon unreachable" verdict -- never let these tests
    depend on whether the box they run on happens to have ollama listening."""
    monkeypatch.setattr("maestro.providers.ollama.fetch_models",
                        lambda *a, **kw: (None, "connection refused"))


def test_runner_action_notifies_when_no_ticket_selected(seeded_home, monkeypatch):
    """The `key is None` guard the binding sweep exercises for every key: pressing
    'o' with nothing selected notifies instead of pushing a screen."""
    _unreachable_ollama(monkeypatch)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # the initial cursor move auto-selects the first row -- clear it
            # explicitly, same as test_tui.py's mocked guard tests, so this
            # actually exercises the `key is None` branch.
            app._selected_key = None
            notifications_before = len(app._notifications)
            await app.run_action("runner")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert len(app._notifications) > notifications_before
            assert app._exception is None

    asyncio.run(_inner())


def test_runner_modal_open_and_escape(seeded_home, monkeypatch):
    _unreachable_ollama(monkeypatch)
    _run_modal_test(seeded_home, "T-5", "runner", _RunnerModal)


def test_runner_modal_round_trip_updates_spec_for_editable_ticket(seeded_home, monkeypatch):
    """AC1: T-5 (ready, no worktree, no live claim) is still editable --
    setting #runner-kind/#runner-model and submitting appends the runner:/
    runner_model: front-matter lines to spec.md on disk."""
    _unreachable_ollama(monkeypatch)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("runner")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _RunnerModal)
            modal.query_one("#runner-kind", Select).value = "opencode"
            modal.query_one("#runner-model", Input).value = "qwen3-coder:30b"
            await modal.run_action("submit")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())
    text = store.spec_path(seeded_home, "T-5").read_text()
    assert "runner: opencode" in text
    assert "runner_model: qwen3-coder:30b" in text


def test_runner_modal_noop_for_non_editable_ticket(seeded_home, monkeypatch):
    """AC7 (T-54): a CONFIRMED-live claim on T-3 -- a reconciler session
    actually in flight, its spawn args (runner included) already frozen at
    launch -- makes the same flow leave spec.md bytes unchanged and grow
    notifications instead of writing."""
    _unreachable_ollama(monkeypatch)
    before_bytes = store.spec_path(seeded_home, "T-3").read_bytes()

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        claims.write_claim(seeded_home, "T-3", proc.pid, "reconcile-T-3")

        async def _inner():
            app = _make_app(seeded_home)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app._selected_key = "T-3"
                await app.run_action("runner")
                await pilot.pause()
                modal = app.screen_stack[-1]
                assert isinstance(modal, _RunnerModal)
                modal.query_one("#runner-kind", Select).value = "opencode"
                notifications_before = len(app._notifications)
                await modal.run_action("submit")
                await pilot.pause()
                assert app._exception is None
                assert len(app._notifications) > notifications_before

        asyncio.run(_inner())
        assert store.spec_path(seeded_home, "T-3").read_bytes() == before_bytes
    finally:
        proc.terminate()
        proc.wait()


def test_runner_modal_populates_model_select_from_catalogue(seeded_home, monkeypatch):
    """When the ollama daemon IS reachable, #runner-model is a Select populated
    with the tool-capable model catalogue (UX-1's `ollama_mod.model_names`) --
    only the tool-capable model shows up, the embed-only one is filtered out."""
    monkeypatch.setattr(
        "maestro.providers.ollama.fetch_models",
        lambda *a, **kw: ([{"name": "qwen3-coder:30b", "capabilities": ["tools"]},
                           {"name": "embed-only", "capabilities": []}], None),
    )

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("runner")
            await pilot.pause()
            modal = app.screen_stack[-1]
            model_sel = modal.query_one("#runner-model", Select)
            names = {str(value) for _prompt, value in model_sel._options}
            assert "qwen3-coder:30b" in names
            assert "embed-only" not in names
            assert app._exception is None

    asyncio.run(_inner())


def test_runner_modal_warns_when_daemon_unreachable(seeded_home, monkeypatch):
    """UX-2 Note: daemon unreachable -> free-text Input fallback + a warning notify,
    the same soft-validation pattern the modal already uses elsewhere."""
    _unreachable_ollama(monkeypatch)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            notifications_before = len(app._notifications)
            await app.run_action("runner")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal.query_one("#runner-model"), Input)
            assert len(app._notifications) > notifications_before
            assert app._exception is None

    asyncio.run(_inner())


def test_runner_kind_switch_to_claude_clears_model_field(seeded_home, monkeypatch):
    """Copies _CreateModal.on_select_changed's reshape: flipping runner-kind
    back to 'claude' clears whatever the model field held."""
    _unreachable_ollama(monkeypatch)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("runner")
            await pilot.pause()
            modal = app.screen_stack[-1]
            model_inp = modal.query_one("#runner-model", Input)
            model_inp.value = "qwen3-coder:30b"
            kind_sel = modal.query_one("#runner-kind", Select)
            # setting .value alone does not emit Select.Changed in Textual 8 --
            # drive on_select_changed directly, same as test_create_modal_new_prefix_reveals_input.
            # _RunnerModal.on_select_changed is async (T-61: it may remove/mount
            # the model field's widget across a runner-kind swap).
            await modal.on_select_changed(Select.Changed(select=kind_sel, value="claude"))
            await pilot.pause()
            assert model_inp.value == ""
            assert app._exception is None

    asyncio.run(_inner())


# --- T-61 (PI-9): pi joins the runner-kind Select, reshapes #runner-model -----

def _unreachable_pi(monkeypatch):
    """Deterministic "pi unreachable" verdict -- never depend on whether the
    box running these tests happens to have a real `pi` on PATH."""
    monkeypatch.setattr("maestro.providers.pi.fetch_models",
                        lambda *a, **kw: (None, "no such file: pi"))


def test_runner_options_include_pi(seeded_home, monkeypatch):
    _unreachable_ollama(monkeypatch)
    _unreachable_pi(monkeypatch)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("runner")
            await pilot.pause()
            modal = app.screen_stack[-1]
            kind_sel = modal.query_one("#runner-kind", Select)
            names = {str(value) for _prompt, value in kind_sel._options}
            assert names == {"claude", "opencode", "pi"}
            assert app._exception is None

    asyncio.run(_inner())


def test_selecting_pi_reshapes_model_field_to_pis_catalogue(seeded_home, monkeypatch):
    """AC8: selecting `pi` reshapes `#runner-model` to pi's own catalogue --
    never ollama's -- proven by MOUNTING the real app and driving a real
    `on_select_changed`."""
    _unreachable_ollama(monkeypatch)
    monkeypatch.setattr(
        "maestro.providers.pi.fetch_models",
        lambda *a, **kw: ([{"provider": "zai", "model": "glm-5.2", "context": "1.0M",
                            "max-out": "128K", "thinking": "yes", "images": "no"},
                           {"provider": "zai", "model": "glm-9.9", "context": "1.0M",
                            "max-out": "128K", "thinking": "yes", "images": "no"}], None))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("runner")
            await pilot.pause()
            modal = app.screen_stack[-1]
            # T-5 opens claude-default -- ollama unreachable -> Input fallback.
            assert isinstance(modal.query_one("#runner-model"), Input)

            kind_sel = modal.query_one("#runner-kind", Select)
            await modal.on_select_changed(Select.Changed(select=kind_sel, value="pi"))
            await pilot.pause()

            model_sel = modal.query_one("#runner-model", Select)
            names = {str(value) for _prompt, value in model_sel._options} - {"Select.NULL"}
            assert names == {"glm-5.2", "glm-9.9"}
            assert app._exception is None

    asyncio.run(_inner())


def test_selecting_pi_with_pi_unreachable_falls_back_to_input_and_warns(seeded_home, monkeypatch):
    _unreachable_ollama(monkeypatch)
    _unreachable_pi(monkeypatch)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("runner")
            await pilot.pause()
            modal = app.screen_stack[-1]
            kind_sel = modal.query_one("#runner-kind", Select)
            notifications_before = len(app._notifications)
            await modal.on_select_changed(Select.Changed(select=kind_sel, value="pi"))
            await pilot.pause()

            assert isinstance(modal.query_one("#runner-model"), Input)
            assert len(app._notifications) > notifications_before
            assert app._exception is None

    asyncio.run(_inner())


def test_switching_from_pi_back_to_opencode_restores_ollamas_catalogue(seeded_home, monkeypatch):
    monkeypatch.setattr(
        "maestro.providers.ollama.fetch_models",
        lambda *a, **kw: ([{"name": "qwen3-coder:30b", "capabilities": ["tools"]}], None))
    monkeypatch.setattr(
        "maestro.providers.pi.fetch_models",
        lambda *a, **kw: ([{"provider": "zai", "model": "glm-5.2", "context": "1.0M",
                            "max-out": "128K", "thinking": "yes", "images": "no"}], None))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("runner")
            await pilot.pause()
            modal = app.screen_stack[-1]
            kind_sel = modal.query_one("#runner-kind", Select)

            await modal.on_select_changed(Select.Changed(select=kind_sel, value="pi"))
            await pilot.pause()
            names = {str(v) for _p, v in modal.query_one("#runner-model", Select)._options} - {"Select.NULL"}
            assert names == {"glm-5.2"}

            await modal.on_select_changed(Select.Changed(select=kind_sel, value="opencode"))
            await pilot.pause()
            names = {str(v) for _p, v in modal.query_one("#runner-model", Select)._options} - {"Select.NULL"}
            assert names == {"qwen3-coder:30b"}
            assert app._exception is None

    asyncio.run(_inner())


def test_runner_modal_opens_directly_on_pi_reshapes_from_compose(seeded_home, monkeypatch):
    """A ticket whose spec already names `runner: pi` opens the modal with the
    model field ALREADY shaped to pi's catalogue -- not ollama's -- at
    `compose()` time, not just after a later kind switch."""
    _unreachable_ollama(monkeypatch)
    monkeypatch.setattr(
        "maestro.providers.pi.fetch_models",
        lambda *a, **kw: ([{"provider": "zai", "model": "glm-5.2", "context": "1.0M",
                            "max-out": "128K", "thinking": "yes", "images": "no"}], None))
    store.atomic_write(store.spec_path(seeded_home, "T-5"),
                       "# T-5\napproval_tier: 1\nrunner: pi\nrunner_model: glm-5.2\n\n"
                       "## Intent\nready ticket\n")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("runner")
            await pilot.pause()
            modal = app.screen_stack[-1]
            kind_sel = modal.query_one("#runner-kind", Select)
            assert kind_sel.value == "pi"
            model_sel = modal.query_one("#runner-model", Select)
            assert model_sel.value == "glm-5.2"
            assert app._exception is None

    asyncio.run(_inner())


def test_runner_modal_round_trip_updates_spec_for_pi(seeded_home, monkeypatch):
    """The full submit path stores `runner: pi`/`runner_model:` verbatim,
    same as the existing opencode round-trip."""
    _unreachable_ollama(monkeypatch)
    monkeypatch.setattr(
        "maestro.providers.pi.fetch_models",
        lambda *a, **kw: ([{"provider": "zai", "model": "glm-5.2", "context": "1.0M",
                            "max-out": "128K", "thinking": "yes", "images": "no"}], None))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("runner")
            await pilot.pause()
            modal = app.screen_stack[-1]
            kind_sel = modal.query_one("#runner-kind", Select)
            await modal.on_select_changed(Select.Changed(select=kind_sel, value="pi"))
            await pilot.pause()
            modal.query_one("#runner-kind", Select).value = "pi"
            modal.query_one("#runner-model", Select).value = "glm-5.2"
            await modal.run_action("submit")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())
    text = store.spec_path(seeded_home, "T-5").read_text()
    assert "runner: pi" in text
    assert "runner_model: glm-5.2" in text


def test_detail_screen_shows_runner_row(seeded_home, monkeypatch):
    """AC4: DetailScreen (screens.py's own call site) renders the Runner row too."""
    _unreachable_ollama(monkeypatch)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("focus_detail")
            await pilot.pause()
            detail_static = app.screen_stack[-1].query_one("#ds-detail", Static)
            content = str(detail_static.content)
            assert "Runner" in content
            assert app._exception is None

    asyncio.run(_inner())
