"""Mounted-app tests for DepsScreen and the Spec view dependencies strip."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable
from rich.text import Text

from conftest import seed_phase, seed_ticket
from maestro import dispatcher as disp_mod, store
from maestro.tui import DepsScreen, DetailScreen, _ConfirmModal
from tui_support import _filter_idx, _make_app


def test_deps_screen_tree_colors_and_navigation(home):
    """D opens DepsScreen: tree of open tickets, done absent, keys colored by depth."""
    seed_phase(home, "A-1", disp_mod.Phase.DONE)
    for key, deps in (("B-1", []), ("B-2", ["B-1"]), ("B-3", ["B-2", "A-1"])):
        seed_phase(home, key, disp_mod.Phase.READY)
        if deps:
            store.spec_path(home, key).write_text(
                f"# {key}\n\ndependsOn: [{', '.join(deps)}]\n\n## Acceptance criteria\n- [ ] ok\n",
                encoding="utf-8")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "B-2"
            await pilot.press("D")
            await pilot.pause()
            await pilot.pause()
            screen = app.screen_stack[-1]
            assert isinstance(screen, DepsScreen)
            tree = screen.query_one("#deps-tree")
            top = tree.root.children
            assert [n.data for n in top] == ["B-1"]
            assert [n.data for n in top[0].children] == ["B-2"]
            assert [n.data for n in top[0].children[0].children] == ["B-3"]
            palette = app.get_css_variables()
            for node, var in ((top[0], "success"), (top[0].children[0], "warning"),
                              (top[0].children[0].children[0], "error")):
                spans = [s for s in node.label.spans if node.data in node.label.plain[s.start:s.end]]
                assert any(palette[var] in str(s.style) for s in spans), (node.data, var)
            assert "A-1" not in "".join(str(n.label) for n in [*top, *top[0].children, *top[0].children[0].children])
            assert screen._current_key() == "B-2"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DepsScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen_stack[-1], DepsScreen)
            assert app._exception is None

    asyncio.run(_inner())


def test_deps_column_counts_and_colors_blocking_deps(home):
    """Deps column: blank when unblocked, open-dep count colored by depth, missing dep blocks."""
    seed_phase(home, "D-0", disp_mod.Phase.DONE)
    for key, deps in (("A-1", []), ("B-1", ["A-1"]), ("C-1", ["B-1"]),
                      ("E-1", ["D-0"]), ("F-1", ["NOPE-9"])):
        seed_phase(home, key, disp_mod.Phase.READY)
        if deps:
            store.spec_path(home, key).write_text(
                f"# {key}\n\ndependsOn: [{', '.join(deps)}]\n\n## Acceptance criteria\n- [ ] ok\n",
                encoding="utf-8")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            col = [c.label.plain for c in table.columns.values()].index("Deps")
            assert [c.label.plain for c in table.columns.values()][col - 1] == "Fails"
            palette = app.get_css_variables()

            def cell(key):
                return table.get_row(key)[col]

            for key in ("A-1", "E-1"):
                assert str(cell(key)) == ""
            for key, color in (("B-1", palette["warning"]), ("C-1", palette["error"]),
                               ("F-1", palette["warning"])):
                c = cell(key)
                assert str(c) == "1" and isinstance(c, Text), key
                assert color in str(c.style), (key, c.style)

            # refresh keeps cursor/selection with the new column
            table.move_cursor(row=table.get_row_index("C-1"))
            await pilot.pause()
            app._refresh_now()
            await pilot.pause()
            assert table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value == "C-1"
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# T-152: Spec view Dependencies strip                                         #
# --------------------------------------------------------------------------- #

def _spec_screen_deps_text(home, key):
    """Open the real SpecScreen via `s` on the main table; return (strip text, shown, exc)."""
    from maestro.tui.screens import SpecScreen

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = key
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], SpecScreen)
            await pilot.press("r")
            await pilot.pause()
            w = app.screen_stack[-1].query_one("#spec-deps")
            return str(w.render()), w.display, app._exception

    return asyncio.run(_inner())


def test_spec_screen_dependencies_strip_shows_status_and_phase(home):
    seed_ticket(home, "D-1", "finished", phase="done")
    seed_ticket(home, "D-2", "waiting", phase="ready")
    seed_ticket(home, "D-3", "busy", phase="implementing")
    seed_ticket(home, "D-0", "child")
    spec = store.spec_path(home, "D-0")
    spec.write_text("# D-0\npriority: 2\ndependsOn: [D-1, D-2, D-3, D-9]\n\n"
                    "## Acceptance criteria\n- [ ] ok\n")
    before = spec.read_bytes()
    text, shown, exc = _spec_screen_deps_text(home, "D-0")
    assert exc is None
    assert shown
    positions = [text.index(s) for s in
                 ("✅ D-1 #done", "💤 D-2 #ready", "⏳ D-3 #implementing", "❓ D-9 #missing")]
    assert positions == sorted(positions)
    assert spec.read_bytes() == before


def test_spec_screen_dependencies_strip_hidden_without_depends_on(home):
    seed_ticket(home, "D-5", "loner")
    _, shown, exc = _spec_screen_deps_text(home, "D-5")
    assert exc is None
    assert not shown


def test_deps_detail_discard_targets_screen_key(seeded_home):
    from textual.widgets import Tree

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            app._selected_key = "T-3"
            await pilot.press("D")
            for _ in range(50):
                await pilot.pause(0.1)
                if isinstance(app.screen, DepsScreen) and app.screen._graph is not None:
                    break
            await pilot.pause()
            tree = app.screen.query_one("#deps-tree", Tree)
            node = next(n for n in tree.root.children if n.data == "T-2")
            tree.move_cursor(node)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen)
            await pilot.press("ctrl+d")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ConfirmModal)
            assert modal._require == "T-2"
            assert app._exception is None

    asyncio.run(_inner())
