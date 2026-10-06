"""T-146: `E` opens the selected ticket's spec.md in $VISUAL/$EDITOR from the main board."""
from __future__ import annotations

import asyncio
import contextlib
import stat

import pytest

pytest.importorskip("textual")

from textual.app import SuspendNotSupported  # noqa: E402
from textual.widgets import Static  # noqa: E402

from maestro import store  # noqa: E402
from maestro.tui import MaestroTUI  # noqa: E402
from maestro.tui.screens import SpecScreen, editor_argv  # noqa: E402


def _stub(tmp_path, body, name="ed"):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def _no_suspend(app):
    """Headless drivers can't suspend; swap in a no-op so the editor really runs."""
    app.suspend = lambda: contextlib.nullcontext()


def _run(home, scenario, *, patch=None):
    async def _inner():
        app = MaestroTUI(home=str(home))
        if patch:
            patch(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await scenario(app, pilot)
            assert app._exception is None

    asyncio.run(_inner())


def _warnings(app):
    return [n for n in app._notifications if n.severity == "warning"]


def test_e_runs_editor_on_selected_spec(seeded_home, tmp_path, monkeypatch):
    stub = _stub(tmp_path, 'echo "# MARKER" >> "$1"\n')
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", str(stub))

    async def scenario(app, pilot):
        key = app._selected_key
        assert key
        await pilot.press("E")
        await pilot.pause()
        assert "# MARKER" in store.spec_path(seeded_home, key).read_text()

    _run(seeded_home, scenario, patch=_no_suspend)


def test_visual_beats_editor_and_multiword_command(tmp_path, monkeypatch):
    spec = tmp_path / "spec.md"
    monkeypatch.setenv("EDITOR", "ed-editor")
    monkeypatch.setenv("VISUAL", "ed-visual --wait")
    assert editor_argv(spec) == ["ed-visual", "--wait", str(spec)]
    monkeypatch.delenv("VISUAL")
    assert editor_argv(spec) == ["ed-editor", str(spec)]
    monkeypatch.delenv("EDITOR")
    assert editor_argv(spec) == ["vi", str(spec)]


def test_multiword_editor_runs_with_flag_before_path(seeded_home, tmp_path, monkeypatch):
    stub = _stub(tmp_path, 'echo "$1" > "$2.args"\necho "# F" >> "$2"\n')
    monkeypatch.setenv("VISUAL", f"{stub} --flag")
    monkeypatch.setenv("EDITOR", "/nonexistent-editor")

    async def scenario(app, pilot):
        spec = store.spec_path(seeded_home, app._selected_key)
        await pilot.press("E")
        await pilot.pause()
        assert (spec.parent / "spec.md.args").read_text().strip() == "--flag"
        assert "# F" in spec.read_text()

    _run(seeded_home, scenario, patch=_no_suspend)


def test_spec_screen_e_uses_same_helper(seeded_home, tmp_path, monkeypatch):
    stub = _stub(tmp_path, 'echo "# SS" >> "$2"\n')
    monkeypatch.setenv("VISUAL", f"{stub} --flag")

    async def scenario(app, pilot):
        key = app._selected_key
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, SpecScreen)
        await pilot.press("e")
        await pilot.pause()
        assert "# SS" in store.spec_path(seeded_home, key).read_text()

    _run(seeded_home, scenario, patch=_no_suspend)


def test_warns_when_no_ticket_selected(seeded_home):
    async def scenario(app, pilot):
        app._selected_key = None
        await app.run_action("edit_spec")
        await pilot.pause()
        assert _warnings(app)

    _run(seeded_home, scenario)


def test_warns_when_spec_missing(seeded_home):
    async def scenario(app, pilot):
        store.spec_path(seeded_home, app._selected_key).unlink()
        await pilot.press("E")
        await pilot.pause()
        assert _warnings(app)

    _run(seeded_home, scenario, patch=_no_suspend)


def test_warns_when_editor_not_found(seeded_home, monkeypatch):
    monkeypatch.setenv("VISUAL", "/nonexistent/editor-xyz")

    async def scenario(app, pilot):
        await pilot.press("E")
        await pilot.pause()
        assert any("not found" in n.message for n in _warnings(app))

    _run(seeded_home, scenario, patch=_no_suspend)


def test_warns_when_terminal_cannot_suspend(seeded_home):
    async def scenario(app, pilot):
        await pilot.press("E")  # headless driver: can_suspend is False
        await pilot.pause()
        assert any("suspend" in n.message for n in _warnings(app))

    _run(seeded_home, scenario)


def test_suspend_not_supported_is_caught(seeded_home):
    def boom():
        raise SuspendNotSupported("nope")

    async def scenario(app, pilot):
        await pilot.press("E")
        await pilot.pause()
        assert _warnings(app)

    _run(seeded_home, scenario, patch=lambda app: setattr(app, "suspend", boom))


def test_detail_pane_rerenders_after_edit(seeded_home, tmp_path, monkeypatch):
    stub = _stub(tmp_path, '{ echo "runner: opencode"; cat "$1"; } > "$1.new" && mv "$1.new" "$1"\n')
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", str(stub))

    async def scenario(app, pilot):
        detail = app.query_one("#detail", Static)
        assert "opencode" not in detail.render().plain
        await pilot.press("E")
        await pilot.pause()
        assert "opencode" in detail.render().plain

    _run(seeded_home, scenario, patch=_no_suspend)
