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
The whole module is skipped when the optional ``tui`` extra (textual) is absent.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="requires the [tui] extra (textual)")

import textual.app as _txapp  # noqa: E402
from textual.widgets import DataTable, Static  # noqa: E402

from maestro.tui import (  # noqa: E402
    EventsScreen,
    FleetScreen,
    LogsScreen,
    MaestroTUI,
    _AnswerModal,
    _CmdModal,
    _CreateModal,
    _FILTERS,
    _IntervalModal,
)


def _make_app(home):
    return MaestroTUI(home=str(home))


def _filter_idx(name: str) -> int:
    return next(i for i, (n, _) in enumerate(_FILTERS) if n == name)


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


def test_row_highlight_renders_every_seeded_phase(seeded_home):
    """Walk the cursor across all rows so on_data_table_row_highlighted renders the
    detail markup for every phase — incl. awaiting-ci, the historical [link=URL] crasher."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            assert table.row_count == 5
            for r in range(table.row_count):
                table.move_cursor(row=r)
                await pilot.pause()
                assert app._exception is None, f"row {r} crashed: {app._exception!r}"

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# (b) EXHAUSTIVE BINDING SWEEP — the structural defense against forgotten cases #
# --------------------------------------------------------------------------- #
# Each key is pressed on its OWN freshly-mounted app, so a crash on one key is a
# captured finding that does not prevent the remaining keys from running (a single
# unhandled exception tears the whole app down in Textual). New bindings added to
# MaestroTUI are swept automatically the day they appear.

# 'q' quits the app cleanly (not a crash) — exercised separately below.
_SWEEP_KEYS = [b[0] for b in MaestroTUI.BINDINGS if b[1] != "quit"]

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


# --------------------------------------------------------------------------- #
# static defense: every binding resolves to a real action method              #
# --------------------------------------------------------------------------- #
# Textual SILENTLY no-ops a key bound to a missing action (run_action returns
# False, no exception) — the press-sweep above cannot see that, so guard it here.

_BINDING_CLASSES = [
    MaestroTUI, EventsScreen, LogsScreen, FleetScreen,
    _AnswerModal, _CmdModal, _IntervalModal, _CreateModal,
]


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
    [(c, b[0], b[1]) for c in _BINDING_CLASSES for b in c.BINDINGS],
    ids=lambda v: getattr(v, "__name__", v),
)
def test_every_binding_action_resolves(owner_cls, key, action):
    assert _action_resolves(owner_cls, action), (
        f"{owner_cls.__name__} binds {key!r} -> action_{action} which does not exist"
    )


# --------------------------------------------------------------------------- #
# (c) open each modal / screen via its action, escape to dismiss              #
# --------------------------------------------------------------------------- #

async def _open_via_action(app, pilot, action, expect_type):
    before = len(app.screen_stack)
    await app.run_action(action)
    await pilot.pause()
    assert app._exception is None, f"{action} crashed: {app._exception!r}"
    top = app.screen_stack[-1]
    assert isinstance(top, expect_type), \
        f"{action} -> {type(top).__name__}, want {expect_type.__name__}"
    assert len(app.screen_stack) == before + 1
    await pilot.press("escape")
    await pilot.pause()
    assert len(app.screen_stack) == before, f"{action} screen did not dismiss"
    assert app._exception is None


def _run_modal_test(seeded_home, selected_key, action, expect_type):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            if selected_key is not None:
                app._selected_key = selected_key
            await _open_via_action(app, pilot, action, expect_type)

    asyncio.run(_inner())


def test_cmd_modal_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-2", "cmd", _CmdModal)  # degraded -> hint branch


def test_cmd_modal_shows_phase_commands(seeded_home):
    """_CmdModal renders phase-specific command rows for degraded, awaiting-human, and generic phases."""
    from maestro.tui import _PHASE_COMMANDS, _DEFAULT_COMMANDS

    cases = [
        ("degraded", _PHASE_COMMANDS["degraded"]),
        ("awaiting-human", _PHASE_COMMANDS["awaiting-human"]),
        ("ready", _DEFAULT_COMMANDS),
    ]

    def _check(phase, expected_commands):
        async def _inner():
            app = _make_app(seeded_home)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                modal = _CmdModal("T-x", phase)
                app.push_screen(modal, lambda _: None)
                await pilot.pause()
                rows = list(app.screen.query("Label.cmd-row"))
                assert len(rows) == len(expected_commands), (
                    f"phase={phase!r}: expected {len(expected_commands)} rows, got {len(rows)}"
                )
                row_texts = [str(lbl.content) for lbl in rows]
                for cmd, _desc in expected_commands:
                    assert any(cmd in t for t in row_texts), (
                        f"phase={phase!r}: cmd {cmd!r} not found in rows {row_texts}"
                    )
                assert app._exception is None
                await pilot.press("escape")
                await pilot.pause()
            assert app._exception is None

        asyncio.run(_inner())

    for phase, cmds in cases:
        _check(phase, cmds)


def test_answer_modal_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-1", "answer", _AnswerModal)  # has open questions


def test_create_modal_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, None, "create", _CreateModal)


def test_create_modal_intent_is_textarea_and_accepts_multiline(seeded_home):
    """Intent field must be a TextArea (not an Input) and must accept newlines."""
    from textual.widgets import Input, TextArea

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


def test_fleet_screen_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, None, "fleet_panel", FleetScreen)


def test_events_screen_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-3", "view_events", EventsScreen)


def test_logs_screen_open_and_escape(seeded_home):
    """LogsScreen runs a thread worker that calls app.call_from_thread — the exact
    surface of the 'call_from_thread is on App, not Screen' regression."""
    _run_modal_test(seeded_home, "T-3", "view_logs", LogsScreen)


def test_interval_modal_inside_fleet_screen(seeded_home):
    """FleetScreen 'u' (fleet_up) opens the _IntervalModal; escape dismisses it."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await pilot.press("u")  # fleet_up -> push _IntervalModal
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _IntervalModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            assert app._exception is None

    asyncio.run(_inner())
