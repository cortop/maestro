"""Runtime tests that MOUNT the TUI through Textual's real event loop.

Unlike ``tests/test_tui.py`` — which tests pure render/data functions or calls
``action_*`` with ``push_screen``/``notify``/``query_one`` mocked — this module
uses ``async with app.run_test() as pilot:`` to actually run ``compose()``,
``on_mount()``, real ``query_one`` lookups, the CSS stylesheet, ``@work`` workers,
real screen pushes, and real key-binding routing. That is exactly the surface
that "crashes during dev for forgotten cases" (e.g. a forgotten widget id, an
``on_mount`` exception, or the recent ``call_from_thread is on App, not Screen``
LogsScreen regression) — none of which the mocked suite can catch.

How crashes are detected: ``run_test`` re-raises the first unhandled exception
from the app's event loop on context exit, and stores it on ``app._exception``
(verified against the installed textual 8.2.7). So after driving the app we
assert ``app._exception is None``. One gap that re-raising does NOT cover: a
binding pointing at a *missing* ``action_*`` method is a silent no-op in Textual,
so ``test_every_binding_action_resolves`` guards that class statically.

No pytest-asyncio dependency: each test is a plain sync function driving an async
inner coroutine via ``asyncio.run()``, keeping the repo's stdlib-only test stack.
The module is not collected when the optional ``tui`` extra (textual) is absent
(``tests/conftest.py``), and every ``test_tui*`` module gets the real-spawn guard from there.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
import textual.app as _txapp
from textual.widgets import DataTable, Input, Select, Static, TextArea
from rich.text import Text

from conftest import seed_ticket
from maestro import config as config_mod, event_log, snapshot as snap_mod, store
from maestro.tui import MaestroTUI, _FILTERS, _ScheduleModal, _styled_row
from tui_support import _BINDING_CLASSES, _baction, _bkey, _filter_idx, _make_app


# --------------------------------------------------------------------------- #
# (a) the app actually mounts                                                  #
# --------------------------------------------------------------------------- #

def test_app_mounts_clean(seeded_home):
    """compose() + on_mount() + first _populate()/_refresh_badge() run without error."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # the ids the rest of tui.py queries must really exist in compose()
            assert app.query_one("#tickets", DataTable).row_count >= 1
            app.query_one("#filter-bar", Static)
            app.query_one("#detail", Static)
            assert app._exception is None
        assert app.return_code in (None, 0)

    asyncio.run(_inner())


def test_filter_bar_renders_on_its_own_visible_row(seeded_home):
    """Regression: the filter bar was invisible because #filter-bar landed on the
    same top row as the docked Header (which, being pinned to a named layer, never
    reserved a flow row) and was painted over. Assert on the RENDERED region — not
    just markup content — so a re-collapse is caught: the bar must own a non-zero
    row of its own, strictly below the header and not overlapping it."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            bar = app.query_one("#filter-bar", Static)
            header = app.query_one("Header")
            table = app.query_one("#tickets", DataTable)

            assert bar.region.height >= 1, "filter bar collapsed to zero height"
            assert bar.region.y > header.region.y, "filter bar not below the header"
            # no vertical overlap with the header row, and above the table
            assert bar.region.y >= header.region.y + header.region.height
            assert table.region.y >= bar.region.y + bar.region.height

    asyncio.run(_inner())


def test_filter_bar_marks_active_filter_unambiguously(seeded_home):
    """The active filter must be distinguishable beyond bold alone (reverse-video
    chip) since bold-only styling was reported as not visibly showing up — the
    inactive entries are dimmed for contrast. Drives the real 'f' binding."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            bar = app.query_one("#filter-bar", Static)

            for expected_idx, (fname, _phases) in enumerate(_FILTERS):
                assert app._filter_idx == expected_idx
                content = str(bar.content)
                assert f"[reverse bold] {fname}(" in content
                for other_name, _ in _FILTERS:
                    if other_name != fname:
                        assert f"[dim]{other_name}(" in content
                await pilot.press("f")
                await pilot.pause()

            assert app._exception is None

    asyncio.run(_inner())


def test_row_highlight_renders_every_seeded_phase(seeded_home):
    """Walk the cursor across all rows so on_data_table_row_highlighted renders the
    detail markup for every phase — incl. awaiting-ci, the historical [link=URL] crasher."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            assert table.row_count == 5
            for r in range(table.row_count):
                table.move_cursor(row=r)
                await pilot.pause()
                assert app._exception is None, f"row {r} crashed: {app._exception!r}"

    asyncio.run(_inner())


def test_all_view_orders_rows_by_phase_attention_priority(home):
    """T-106: opening the 'all' filter shows awaiting-human tickets first and
    done tickets last, honoring the spec's five anchors -- awaiting-human > qa
    > implementing > ready > done -- against the real mounted DataTable, not a
    mocked table."""
    seed_ticket(home, "T-5", "done ticket", phase="done")
    seed_ticket(home, "T-4", "ready ticket", phase="ready")
    seed_ticket(home, "T-3", "implementing ticket", phase="implementing")
    seed_ticket(home, "T-2", "qa ticket", phase="qa")
    seed_ticket(home, "T-1", "awaiting-human ticket", phase="awaiting-human",
                questions={"q1": "ok?"})

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            assert table.row_count == 5
            keys = [str(table.get_row_at(r)[0]) for r in range(table.row_count)]
            assert keys == ["T-1", "T-2", "T-3", "T-4", "T-5"]
            assert app._exception is None

    asyncio.run(_inner())


def test_detail_panel_resize_bindings_grow_shrink_and_clamp(seeded_home):
    """T-107: '[' / ']' resize the #tickets/#right split live, clamp at both
    ends instead of hiding either panel or crashing, and the chosen ratio
    survives a periodic _populate() refresh within the session."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            assert table.styles.width.value == 2.0

            # ']' widens the detail column (shrinks #tickets), clamped at MIN.
            await pilot.press("]")
            await pilot.pause()
            assert table.styles.width.value == 1.5
            for _ in range(5):
                await pilot.press("]")
                await pilot.pause()
            assert app._tickets_fr == app._TICKETS_FR_MIN
            assert table.styles.width.value == app._TICKETS_FR_MIN
            assert table.region.width > 0, "tickets panel vanished"
            assert app._exception is None

            # '[' narrows the detail column (grows #tickets), clamped at MAX.
            for _ in range(10):
                await pilot.press("[")
                await pilot.pause()
            assert app._tickets_fr == app._TICKETS_FR_MAX
            assert table.styles.width.value == app._TICKETS_FR_MAX
            right = app.query_one("#right")
            assert right.region.width > 0, "detail panel vanished"
            assert app._exception is None

            # The ratio survives a periodic refresh within the session.
            app._refresh_now()
            await pilot.pause()
            assert app._tickets_fr == app._TICKETS_FR_MAX
            assert app.query_one("#tickets", DataTable).styles.width.value == \
                app._TICKETS_FR_MAX
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# (b) EXHAUSTIVE BINDING SWEEP — the structural defense against forgotten cases #
# --------------------------------------------------------------------------- #
# Each key is pressed on its OWN freshly-mounted app, so a crash on one key is a
# captured finding that does not prevent the remaining keys from running (a single
# unhandled exception tears the whole app down in Textual). New bindings added to
# MaestroTUI are swept automatically the day they appear.

# 'q' quits the app cleanly (not a crash) — exercised separately below.
_SWEEP_KEYS = [_bkey(b) for b in MaestroTUI.BINDINGS if _baction(b) != "quit"]


# Keys that are a genuine, captured crash → xfail so the rest still run.
_KNOWN_CRASH: dict[str, str] = {}


@pytest.mark.parametrize("key", _SWEEP_KEYS)
def test_binding_key_does_not_crash(seeded_home, key):
    """Press one binding key on a mounted app; assert nothing propagates."""
    if key in _KNOWN_CRASH:
        pytest.xfail(_KNOWN_CRASH[key])

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            # dismiss any modal/screen the key opened, then settle
            await pilot.press("escape")
            await pilot.pause()
            assert app._exception is None, (
                f"binding {key!r} crashed the app: {app._exception!r}"
            )

    asyncio.run(_inner())


def test_binding_sweep_all_in_one(seeded_home):
    """Belt-and-suspenders: iterate BINDINGS in a single test, collecting every
    crash so a failing run reports ALL offending keys at once (not just the first)."""
    async def _inner():
        crashes: dict[str, str] = {}
        for key in _SWEEP_KEYS:
            app = _make_app(seeded_home)
            try:
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press(key)
                    await pilot.pause()
                    await pilot.press("escape")
                    await pilot.pause()
                    if app._exception is not None:
                        crashes[key] = repr(app._exception)
            except Exception as exc:  # run_test re-raises the captured panic on exit
                crashes[key] = repr(exc)
        return crashes

    crashes = asyncio.run(_inner())
    surprising = {k: v for k, v in crashes.items() if k not in _KNOWN_CRASH}
    assert not surprising, f"binding keys crashed: {surprising}"


def test_quit_binding_exits_clean(seeded_home):
    """'q' shuts the app down cleanly (return_code 0/None, no exception)."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        assert app._exception is None
        assert app.return_code in (None, 0)

    asyncio.run(_inner())


def test_binding_classes_are_discovered():
    """_BINDING_CLASSES is found by walking maestro.tui, not hand-listed (T-187)."""
    names = {c.__name__ for c in _BINDING_CLASSES}
    assert {"MaestroTUI", "LogsScreen", "_CmdModal", "_SessionPickModal"} <= names
    assert [c.__qualname__ for c in _BINDING_CLASSES] == sorted(c.__qualname__ for c in _BINDING_CLASSES)
    assert all(c.BINDINGS for c in _BINDING_CLASSES)


def _action_resolves(owner_cls, action: str) -> bool:
    name = action.split("(", 1)[0]  # strip any parameters
    ns, sep, meth = name.partition(".")
    if sep:  # namespaced, e.g. "app.pop_screen"
        target = _txapp.App if ns in ("app", "screen") else owner_cls
        meth_name = meth
    else:
        target, meth_name = owner_cls, name
    return hasattr(target, f"action_{meth_name}")


@pytest.mark.parametrize(
    "owner_cls,key,action",
    [(c, _bkey(b), _baction(b)) for c in _BINDING_CLASSES for b in c.BINDINGS],
    ids=lambda v: getattr(v, "__name__", v),
)
def test_every_binding_action_resolves(owner_cls, key, action):
    assert _action_resolves(owner_cls, action), (
        f"{owner_cls.__name__} binds {key!r} -> action_{action!r} which does not exist"
    )


def test_needs_you_filter_shows_only_sleeping_and_stuck_phases(home):
    """AD-7 mounted-app QA: the needs-you filter -- cycled to with real `f`
    presses, not just relying on it being the default -- shows only the two
    sleeping-and-stuck phases (awaiting-human/degraded); an ordinary
    implementing ticket never appears there (the tier-2 gate this filter used
    to also fold in is gone -- there is no way to be "gated" anymore)."""
    seed_ticket(home, "G-1", "waiting on a human", phase="awaiting-human")
    seed_ticket(home, "G-2", "ordinary change", phase="implementing")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()  # first _populate()

            # Cycle all the way around with real keypresses -- proves `f` actually
            # lands back on needs-you, not just that index 0 defaults to it.
            for _ in range(len(_FILTERS)):
                await pilot.press("f")
                await pilot.pause()
            assert _FILTERS[app._filter_idx][0] == "needs-you"
            assert app._exception is None

            table = app.query_one("#tickets", DataTable)
            keys = [str(table.get_row_at(r)[0]) for r in range(table.row_count)]
            assert "G-1" in keys, f"awaiting-human ticket missing from needs-you filter: {keys}"
            assert "G-2" not in keys, f"implementing ticket wrongly in needs-you filter: {keys}"

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# (d) TUI-13: phase styling and live notifications                             #
# --------------------------------------------------------------------------- #

def test_styled_row_returns_rich_text_for_attention_phases():
    """_styled_row wraps cells in styled Text for awaiting-human and degraded."""
    for phase in ("awaiting-human", "degraded"):
        row = _styled_row("T-1", phase, "title", "—", "—", "1", "0")
        assert all(isinstance(c, Text) for c in row), (
            f"phase={phase!r}: expected all Text cells, got {[type(c).__name__ for c in row]}"
        )
    # Triaging / ready — no style wrapping, cells pass through as-is
    plain = _styled_row("T-2", "triaging", "title", "—", "—", "1", "0")
    assert not any(isinstance(c, Text) for c in plain), (
        f"triaging: expected no Text wrapping, got {[type(c).__name__ for c in plain]}"
    )


def test_styled_row_preserves_pr_link_markup():
    """L-11: the PR cell's [link=...] markup must survive phase styling so the
    main-view table cell stays clickable (a literal Text() would show the raw
    markup text instead of a link, since it isn't markup-parsed).

    Asserts the *rendered* OSC 8 URI, not just that a link span exists: an
    earlier version quoted the URL (`[link="..."]`) and Rich carried the quotes
    into the hyperlink target, so the terminal received `"https://…"` — a
    malformed URL that would not open. Checking the emitted URI catches that.
    """
    from rich.console import Console
    from textual.strip import Strip

    url = "https://github.com/x/y/pull/42"
    pr_cell = f"[link={url}]#42[/link]"
    row = _styled_row("T-1", "awaiting-ci", "title", pr_cell, "passing", "1", "0")
    pr_text = row[3]
    assert isinstance(pr_text, Text)
    assert pr_text.plain == "#42"

    console = Console()
    rendered = Strip(list(pr_text.render(console))).render(console)
    m = re.search(r"\x1b]8;[^;]*;([^\x1b]*)", rendered)
    assert m, f"expected an OSC 8 hyperlink in rendered output: {rendered!r}"
    assert m.group(1) == url, f"link URI must be the bare URL, got {m.group(1)!r}"


def test_phase_styled_rows_render_without_crash(seeded_home):
    """DataTable populated with styled Text cells mounts and renders without error."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            table = app.query_one(DataTable)
            assert table.row_count >= 1
            assert app._exception is None

    asyncio.run(_inner())


def test_no_notification_on_first_populate(seeded_home):
    """First _populate() sets the baseline; tickets already in awaiting-human/degraded
    do not fire notifications (would be noisy on startup)."""
    async def _inner():
        app = _make_app(seeded_home)
        # _prev_phases is None before the app is mounted
        assert app._prev_phases is None
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()  # first _populate() runs via on_mount
            # Baseline captured — no notification for pre-existing phases
            assert isinstance(app._prev_phases, dict)
            assert len(app._notifications) == 0, (
                f"Expected no notifications on startup, got {list(app._notifications)}"
            )
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# (e) TUI-19: bottom bar shows a reduced set of shortcuts                     #
# --------------------------------------------------------------------------- #

def test_footer_binding_count_is_reduced():
    """show=False hides low-priority shortcuts; at most 10 visible in the footer.

    (TUI-19 set the original budget at 8; T-107 raised it to 10 to surface the
    two panel-resize bindings ('[' / ']') in the footer alongside the other
    primary actions, per its own AC — still a deliberate, bounded budget, not
    the pre-TUI-19 17-binding clutter.)
    """
    from textual.binding import Binding
    visible = [
        b for b in MaestroTUI.BINDINGS
        if not isinstance(b, Binding) or b.show
    ]
    assert len(visible) <= 10, (
        f"Too many visible footer bindings ({len(visible)}): "
        + ", ".join(_bkey(b) for b in visible)
    )


def test_hidden_binding_keys_still_work(seeded_home):
    """show=False shortcuts (e.g. 's', 'l') still fire their actions without crashing."""
    from textual.binding import Binding
    hidden_keys = [_bkey(b) for b in MaestroTUI.BINDINGS
                   if isinstance(b, Binding) and not b.show and _baction(b) != "quit"]

    async def _inner():
        crashes: dict[str, str] = {}
        for key in hidden_keys:
            app = _make_app(seeded_home)
            try:
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    app._selected_key = "T-3"
                    await pilot.press(key)
                    await pilot.pause()
                    await pilot.press("escape")
                    await pilot.pause()
                    if app._exception is not None:
                        crashes[key] = repr(app._exception)
            except Exception as exc:
                crashes[key] = repr(exc)
        return crashes

    crashes = asyncio.run(_inner())
    assert not crashes, f"hidden-binding keys crashed: {crashes}"


def test_notification_fires_on_phase_transition(seeded_home):
    """Second _populate() fires a warning notification when a ticket newly enters
    awaiting-human or degraded (phase change detected vs prev snapshot)."""
    from maestro import event_log as elog

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()  # first populate; sets baseline
            assert app._prev_phases is not None

            # Simulate T-5 (was "ready") transitioning to "awaiting-human"
            elog.append(seeded_home, "T-5", "PhaseChanged",
                        {"phase": "awaiting-human", "reason": "test"}, actor="test")
            snap_mod.rebuild(seeded_home, "T-5")

            notifications_before = len(app._notifications)
            app._refresh_now()
            await pilot.pause()
            assert len(app._notifications) > notifications_before, (
                "Expected a warning notification after T-5 entered awaiting-human"
            )
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# (f) RT-4: kind/model/effort selectors, researching style, proposal viewer   #
# --------------------------------------------------------------------------- #

def test_researching_phase_in_phase_style():
    """_PHASE_STYLE must contain 'researching' with a non-empty style."""
    from maestro.tui import _PHASE_STYLE
    assert "researching" in _PHASE_STYLE, "researching phase not in _PHASE_STYLE"
    assert _PHASE_STYLE["researching"], "researching style must be non-empty"


def test_researching_rows_render_without_crash(seeded_home):
    """DataTable with a researching-phase ticket mounts and renders without error."""

    event_log.append(seeded_home, "T-5", "PhaseChanged",
                     {"phase": "researching", "reason": "test"}, actor="test")
    snap_mod.rebuild(seeded_home, "T-5")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())


def test_verifying_phase_in_phase_style():
    """RB-14: _PHASE_STYLE must contain 'verifying' with a non-empty style."""
    from maestro.tui import _PHASE_STYLE
    assert "verifying" in _PHASE_STYLE, "verifying phase not in _PHASE_STYLE"
    assert _PHASE_STYLE["verifying"], "verifying style must be non-empty"


def test_verifying_rows_render_without_crash(seeded_home):
    """RB-14: DataTable with a verifying-phase ticket mounts and renders without error."""

    event_log.append(seeded_home, "T-5", "PhaseChanged",
                     {"phase": "verifying", "reason": "test"}, actor="test")
    snap_mod.rebuild(seeded_home, "T-5")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())


def test_no_select_blank_sentinel_in_tui():
    """Textual 8: Select.BLANK is Widget.BLANK (False), not the no-selection sentinel."""

    import maestro

    offenders = [str(p) for p in Path(maestro.__file__).parent.rglob("*.py")
                 if "Select.BLANK" in p.read_text()]
    assert offenders == []


def test_select_kind_values_survive_null_sentinel_switch(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("create")
            await pilot.pause()
            modal = app.screen_stack[-1]
            modal.query_one("#create-title", Input).value = "Default kind"
            modal.query_one("#create-prefix", Select).value = "T"
            await pilot.pause()
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

            await app.run_action("schedule_panel")
            await pilot.pause()
            await app.screen_stack[-1].run_action("add_task")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ScheduleModal)
            modal.query_one("#sched-name", Input).value = "kind-task"
            modal.query_one("#sched-prompt", TextArea).text = "Do it"
            modal.query_one("#sched-every", Input).value = "6h"
            modal.query_one("#sched-kind", Select).value = "research"
            await modal.run_action("submit")
            await pilot.pause()
            assert app._exception is None

        lines = store.new_inbox_path(seeded_home).read_text().splitlines()
        last = [json.loads(line) for line in lines if line.strip()][-1]
        assert last["title"] == "Default kind"
        assert last["args"]["kind"] == "implementation"

    asyncio.run(_inner())
    cfg = config_mod.load(str(seeded_home))
    assert cfg.scheduled[0]["kind"] == "research"
