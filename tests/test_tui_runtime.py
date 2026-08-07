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
import re
import subprocess
import sys

import pytest

pytest.importorskip("textual", reason="requires the [tui] extra (textual)")

import textual.app as _txapp  # noqa: E402
from textual.widgets import DataTable, Input, Select, Static, TextArea  # noqa: E402

from rich.text import Text  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import claims, config as config_mod, event_log, inbox  # noqa: E402
from maestro import ops as ops_mod, snapshot as snap_mod, store  # noqa: E402
from maestro.cli import main as cli_main  # noqa: E402
from maestro.tui import (  # noqa: E402
    DetailScreen,
    EventsScreen,
    FleetScreen,
    LogsScreen,
    MaestroTUI,
    ProposalScreen,
    ScheduleScreen,
    _AnswerModal,
    _CmdModal,
    _CreateModal,
    _FILTERS,
    _InboxModal,
    _IntervalModal,
    _ScheduleModal,
    _styled_row,
)


def _make_app(home):
    return MaestroTUI(home=str(home))


def _filter_idx(name: str) -> int:
    return next(i for i, (n, _) in enumerate(_FILTERS) if n == name)


# BINDINGS entries can be plain tuples or Binding dataclass instances.
def _bkey(b) -> str:
    return b.key if hasattr(b, "key") else b[0]

def _baction(b) -> str:
    return b.action if hasattr(b, "action") else b[1]


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


# --------------------------------------------------------------------------- #
# static defense: every binding resolves to a real action method              #
# --------------------------------------------------------------------------- #
# Textual SILENTLY no-ops a key bound to a missing action (run_action returns
# False, no exception) — the press-sweep above cannot see that, so guard it here.

_BINDING_CLASSES = [
    MaestroTUI, DetailScreen, EventsScreen, LogsScreen, FleetScreen, ProposalScreen,
    ScheduleScreen, _AnswerModal, _CmdModal, _IntervalModal, _CreateModal, _InboxModal,
    _ScheduleModal,
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
    [(c, _bkey(b), _baction(b)) for c in _BINDING_CLASSES for b in c.BINDINGS],
    ids=lambda v: getattr(v, "__name__", v),
)
def test_every_binding_action_resolves(owner_cls, key, action):
    assert _action_resolves(owner_cls, action), (
        f"{owner_cls.__name__} binds {key!r} -> action_{action!r} which does not exist"
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
        import json
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

        import json
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


def test_fleet_screen_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, None, "fleet_panel", FleetScreen)


def test_fleet_screen_shows_paused_until_when_rate_limited(seeded_home):
    """A real .ratelimit.json pause is picked up by FleetScreen's fleet-refresh
    worker and rendered into #fleet-status as a 'paused until HH:MM' line."""
    import time as time_mod

    from maestro import store

    until_ts = time_mod.time() + 3600
    store.write_json(seeded_home / "derived" / ".ratelimit.json", {
        "paused_until": until_ts, "resets_at": until_ts - 60,
        "rate_limit_type": "five_hour", "source_key": "T-1",
        "source_log": "x", "ts": store.iso_now(),
    })

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            rendered = app.screen_stack[-1].query_one("#fleet-status", Static).content
            text = rendered if isinstance(rendered, str) else str(rendered)
            assert "paused until" in text
            until_str = time_mod.strftime("%H:%M", time_mod.localtime(until_ts))
            assert until_str in text
            assert app._exception is None

    asyncio.run(_inner())


def test_detail_screen_open_and_escape(seeded_home):
    """Enter on a selected row opens DetailScreen (fullscreen right panel); Escape closes it."""
    _run_modal_test(seeded_home, "T-3", "focus_detail", DetailScreen)


def test_enter_key_on_focused_table_opens_detail(seeded_home):
    """Pressing the real Enter key on the focused DataTable opens DetailScreen.

    A focused DataTable consumes Enter (emitting RowSelected), so the app-level
    `enter` binding never fires — only on_data_table_row_selected reaches the
    detail view. run_action('focus_detail') would mask this, so press the key.
    """
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            before = len(app.screen_stack)
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == before + 1, "Enter did not open a screen"
            assert isinstance(app.screen_stack[-1], DetailScreen)
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == before
            assert app._exception is None

    asyncio.run(_inner())


def test_detail_screen_shows_detail_and_events(seeded_home):
    """DetailScreen mounts, populates #ds-detail and #ds-events without error."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("focus_detail")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            screen = app.screen_stack[-1]
            screen.query_one("#ds-detail", Static)  # widget must exist
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_events_screen_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-3", "view_events", EventsScreen)


def test_logs_screen_open_and_escape(seeded_home):
    """LogsScreen runs a thread worker that calls app.call_from_thread — the exact
    surface of the 'call_from_thread is on App, not Screen' regression."""
    _run_modal_test(seeded_home, "T-3", "view_logs", LogsScreen)


def test_logs_screen_stops_tail_on_denied_claim(seeded_home):
    """A claim whose recorded epoch predates a real, live, non-reconciler process
    (pid reuse) is verified-denied — the tail worker must stop instead of polling
    a genuinely-alive-but-wrong pid forever (T-17). Proved via the real app, not
    a mocked query_one/notify."""
    log_path = seeded_home / "agent-logs" / "T-3" / "reconcile-T-3-1000.000000.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("line one\n", encoding="utf-8")

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.write_json(claims.claim_path(seeded_home, "T-3"),
                         {"pid": proc.pid, "name": "reconcile-T-3",
                          "ts": store.iso_now(), "epoch": store.now_epoch() - 3600,
                          "log_path": str(log_path)})
        _run_modal_test(seeded_home, "T-3", "view_logs", LogsScreen)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_logs_screen_renders_rate_limited_result_not_green(seeded_home):
    """T-18: a session log whose terminal result is is_error/429 must render as an
    error/rate-limit line in the real mounted logs pane, never green success."""
    from pathlib import Path
    from textual.widgets import RichLog

    fixture = Path(__file__).parent / "fixtures" / "rate_limited.stream.jsonl"
    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "reconcile-T-3-9999999999.000000.stream.jsonl").write_bytes(
        fixture.read_bytes()
    )

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("view_logs")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], LogsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()

            log_widget = app.screen.query_one("#logs-view", RichLog)
            rendered = "\n".join(strip.text for strip in log_widget.lines)
            assert "429" in rendered
            assert "rate_limited" in rendered
            assert "success" not in rendered

            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


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


def test_fleet_screen_reports_spawn_rate_from_health_report(seeded_home):
    """FleetScreen._load_status must return health.report(...) verbatim (no
    hand-rolled doctor dict, no second copy of the 1800s staleness threshold):
    mounting the real app and opening FleetScreen renders the spawn-rate line."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = app.screen_stack[-1].query_one("#fleet-status", Static)
            content = str(status.content)
            assert "Spawns/hr" in content
            assert "Runaway" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_renders_runaway_board_differently(seeded_home):
    """A board that has actually exceeded its spawn budget must render visibly
    differently (RUNAWAY, red) from the healthy seeded_home above."""
    from maestro import dispatcher as disp
    from maestro.config import Config
    from maestro.statemachine import Phase
    from test_dispatcher import _EphemeralSessions, _seed

    (seeded_home / "config.toml").write_text(
        "[maestro]\nrunaway_spawns_per_hour = 1\n")
    _seed(seeded_home, "R-1", Phase.IN_REVIEW)
    cfg = Config(home=seeded_home, max_concurrency=1, min_spawn_interval=0)
    sessions = _EphemeralSessions()
    t0 = store.now_epoch()
    for i in range(3):
        disp.dispatch(cfg, sessions, now=t0 + i)  # 3 spawns > budget of 1

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = app.screen_stack[-1].query_one("#fleet-status", Static)
            content = str(status.content)
            assert "RUNAWAY" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_header_badge_shows_paused_state(seeded_home):
    """AC (T-15): the header badge reflects a paused board."""
    from maestro import fleet

    fleet.pause(seeded_home, reason="tui check")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause(0.1)  # let the threaded badge worker land
            badge = app.query_one("#fleet-badge", Static)
            assert "PAUSED" in str(badge.content)
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_shows_paused_and_toggle_resumes(seeded_home):
    """AC (T-15): FleetScreen surfaces the paused state and the new 'P' binding
    (not 'p' — already project_rebuild in both binding tables) toggles it."""
    from maestro import fleet

    fleet.pause(seeded_home, reason="tui toggle")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await pilot.pause(0.1)  # let the status-load worker land
            status_widget = app.screen.query_one("#fleet-status", Static)
            assert "Paused" in str(status_widget.content)

            await pilot.press("P")  # toggle_pause -> resume (was paused)
            await pilot.pause(0.1)
            assert fleet.pause_state(seeded_home, store.now_epoch()) is None
            assert app._exception is None

            await pilot.press("P")  # toggle_pause -> pause (now unpaused)
            await pilot.pause(0.1)
            assert fleet.pause_state(seeded_home, store.now_epoch()) is not None
            assert app._exception is None

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
            app._populate()
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
    """show=False hides low-priority shortcuts; at most 8 visible in the footer."""
    from textual.binding import Binding
    visible = [
        b for b in MaestroTUI.BINDINGS
        if not isinstance(b, Binding) or b.show
    ]
    assert len(visible) <= 8, (
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
    from maestro import event_log as elog, snapshot as snap_mod

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
            app._populate()
            await pilot.pause()
            assert len(app._notifications) > notifications_before, (
                "Expected a warning notification after T-5 entered awaiting-human"
            )
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# (e) TUI-18: inbox-compose action works at any phase                          #
# --------------------------------------------------------------------------- #

def test_inbox_modal_open_and_escape(seeded_home):
    """'i' key opens _InboxModal on a selected ticket; escape dismisses it."""
    _run_modal_test(seeded_home, "T-3", "inbox_message", _InboxModal)


def test_inbox_message_writes_to_inbox(seeded_home):
    """Submitting _InboxModal appends a 'msg' command to the ticket inbox."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("inbox_message")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _InboxModal), "inbox modal did not open"
            modal = app.screen_stack[-1]
            inp = modal.query_one("#inbox-input", Input)
            inp.value = "hello from TUI-18 test"
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == 1, "modal should have dismissed after submit"
            assert app._exception is None

        # Verify the inbox entry was written
        from maestro import inbox
        entries = inbox.pending(seeded_home, "T-3")
        assert entries, "no entry written to T-3 inbox"
        last = entries[-1]
        assert last["command"] == "msg"
        assert last["args"]["text"] == "hello from TUI-18 test"

    asyncio.run(_inner())


def test_inbox_action_works_for_any_phase(seeded_home):
    """inbox_message action is accessible regardless of the ticket's current phase."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            for r in range(table.row_count):
                table.move_cursor(row=r)
                await pilot.pause()
                await app.run_action("inbox_message")
                await pilot.pause()
                assert isinstance(app.screen_stack[-1], _InboxModal), (
                    f"row {r}: expected _InboxModal, got {type(app.screen_stack[-1]).__name__}"
                )
                await pilot.press("escape")
                await pilot.pause()
                assert len(app.screen_stack) == 1, f"row {r}: modal did not dismiss"
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
    from maestro import event_log, snapshot as snap_mod

    event_log.append(seeded_home, "T-5", "PhaseChanged",
                     {"phase": "researching", "reason": "test"}, actor="test")
    snap_mod.rebuild(seeded_home, "T-5")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
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

        import json
        new_path = store.new_inbox_path(seeded_home)
        entries = [json.loads(line) for line in new_path.read_text().splitlines() if line.strip()]
        last = entries[-1]
        assert last["title"] == "Research feature"
        assert last.get("args", {}).get("kind") == "research"
        assert last.get("args", {}).get("model") == "opus"
        assert last.get("args", {}).get("effort") == "high"

    asyncio.run(_inner())


def test_proposal_screen_no_proposal_notifies(seeded_home):
    """action_view_proposal on DetailScreen notifies when no proposal.md exists."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("focus_detail")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            screen = app.screen_stack[-1]
            notifs_before = len(app._notifications)
            await screen.run_action("view_proposal")
            await pilot.pause()
            assert len(app._notifications) > notifs_before, "expected a notification when no proposal.md"
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_proposal_screen_opens_with_proposal(seeded_home):
    """action_view_proposal opens ProposalScreen when proposal.md exists."""
    proposal_path = seeded_home / "tickets" / "T-3" / "proposal.md"
    proposal_path.write_text("# Proposal\n\nThis is a test proposal.")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("focus_detail")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            screen = app.screen_stack[-1]
            await screen.run_action("view_proposal")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], ProposalScreen), (
                f"expected ProposalScreen, got {type(app.screen_stack[-1]).__name__}"
            )
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


# --- T-10: scheduled tasks TUI surface ---------------------------------------

def _write_scheduled_config(home, **overrides):
    task = {
        "name": "digest", "prompt": "Summarize things", "every": "1h",
        "approval_tier": 1, "kind": "implementation", "priority": 3,
        "prefix": "S", "enabled": True,
    }
    task.update(overrides)
    config_mod.write_scheduled(home, [task])
    return task


def test_schedule_screen_open_and_escape(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _open_via_action(app, pilot, "schedule_panel", ScheduleScreen)

    asyncio.run(_inner())


def test_schedule_screen_shows_configured_tasks(seeded_home):
    _write_scheduled_config(seeded_home)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            screen = app.screen_stack[-1]
            assert isinstance(screen, ScheduleScreen)
            table = screen.query_one("#schedule-table", DataTable)
            assert table.row_count == 1
            assert app._exception is None

    asyncio.run(_inner())


def test_schedule_modal_open_and_escape(seeded_home):
    _write_scheduled_config(seeded_home)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            schedule_screen = app.screen_stack[-1]
            before = len(app.screen_stack)
            await schedule_screen.run_action("add_task")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _ScheduleModal)
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == before

    asyncio.run(_inner())


def test_schedule_modal_add_task_writes_config(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            schedule_screen = app.screen_stack[-1]
            await schedule_screen.run_action("add_task")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ScheduleModal)
            modal.query_one("#sched-name", Input).value = "new-task"
            modal.query_one("#sched-prompt", TextArea).text = "Do the thing"
            modal.query_one("#sched-every", Input).value = "6h"
            await modal.run_action("submit")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    cfg = config_mod.load(str(seeded_home))
    assert len(cfg.scheduled) == 1
    assert cfg.scheduled[0]["name"] == "new-task"
    assert cfg.scheduled[0]["every"] == "6h"


def test_schedule_toggle_task_flips_enabled(seeded_home):
    _write_scheduled_config(seeded_home, enabled=True)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            screen = app.screen_stack[-1]
            table = screen.query_one("#schedule-table", DataTable)
            table.move_cursor(row=0)
            await pilot.pause()
            await screen.run_action("toggle_task")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    cfg = config_mod.load(str(seeded_home))
    assert cfg.scheduled[0]["enabled"] is False


# --------------------------------------------------------------------------- #
# T-25: frontier rounds in the TUI (answer flow + recommendations)            #
# --------------------------------------------------------------------------- #

def _seed_round(home, key, questions):
    """Seed `key` with a REAL multi-question `maestro ask` round, driven through
    the actual CLI (T-24's `--question` flag over `ops.ask_round`) rather than
    hand-built event payloads -- `questions` is a list of `(text, recommend)`
    pairs, `recommend` may be None."""
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 1\n")
    event_log.append(home, key, "TicketCreated", {"title": key}, actor="d")
    snap_mod.rebuild(home, key)
    args = ["--home", str(home), "ask", key]
    for text, recommend in questions:
        args += ["--question", text, recommend or "", ""]
    rc = cli_main(args)
    assert rc == 0


def _qid_for(home, key, body_prefix):
    """Look up the qid of the open question whose parsed body starts with
    `body_prefix` -- `open_questions` round-trips through a sort_keys=True JSON
    snapshot, so its dict order is qid-alphabetical, not round order; tests must
    not assume `list(...items())` order matches the questions as seeded."""
    snap = snap_mod.load(home, key)
    for qid, text in snap.open_questions.items():
        _, _, body, _ = ops_mod.parse_round_question(text)
        if body.startswith(body_prefix):
            return qid
    raise AssertionError(f"no open question in {key} starts with {body_prefix!r}")


def test_answer_modal_shows_round_position_and_recommendation(home):
    """AC1: the modal shows the question's position in the round ('N of M') and
    surfaces the recommendation as its own labeled section, not squashed into
    the raw '1/2. ...\\n   Recommended: ...' string."""
    _seed_round(home, "T-1", [
        ("Use Postgres or SQLite?", "Postgres (matches prod)"),
        ("Cut a v2 API or extend v1?", None),
    ])

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await app.run_action("answer")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _AnswerModal)
            assert (modal._position, modal._total) == (1, 2)
            assert modal._recommend == "Postgres (matches prod)"

            header = str(modal.query_one("#answer-dialog Label").content)
            assert "1 of 2" in header

            recommend_static = modal.query_one("#recommend-scroll Static")
            assert "Postgres (matches prod)" in str(recommend_static.content)
            # the raw wire-format prefix/suffix must not leak into either widget
            question_static = modal.query_one("#question-scroll Static")
            assert "1/2." not in str(question_static.content)
            assert "Recommended:" not in str(question_static.content)

            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(_inner())


def test_answer_modal_hides_recommend_section_when_absent(home):
    """A question with no recommendation renders no '── Recommended ──' section."""
    _seed_round(home, "T-2", [("Cut a v2 API or extend v1?", None)])

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-2"
            await app.run_action("answer")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert modal._recommend is None
            assert not modal.query("#recommend-scroll")
            assert app._exception is None

    asyncio.run(_inner())


def test_answer_flow_ctrl_r_accepts_recommendation_without_retyping(home):
    """AC2: a single keystroke (Ctrl+R) accepts the shown recommendation as the
    answer, without retyping it; a question with no recommendation is unaffected
    -- Ctrl+R there warns and leaves the modal open for a typed answer."""
    _seed_round(home, "T-3", [
        ("Use Postgres or SQLite?", "Postgres (matches prod)"),
        ("Cut a v2 API or extend v1?", None),
        ("Who owns the migration script?", "the reconciler"),
    ])

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("answer")
            await pilot.pause()

            # Q1 carries a recommendation -- Ctrl+R accepts it and advances.
            assert app.screen_stack[-1]._recommend == "Postgres (matches prod)"
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 2  # main screen + Q2 modal

            # Q2 carries none -- Ctrl+R is a no-op (still open), so type an answer.
            modal2 = app.screen_stack[-1]
            assert modal2._recommend is None
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert app.screen_stack[-1] is modal2, "Ctrl+R with no recommendation must not advance"
            await pilot.press("e", "x", "t", "e", "n", "d", " ", "v", "1")
            await pilot.press("enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 2  # Q3 modal now open

            # Q3 carries a recommendation -- accept it too.
            assert app.screen_stack[-1]._recommend == "the reconciler"
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1  # walk finished, modal closed

    asyncio.run(_inner())

    pending = inbox.pending(home, "T-3")
    assert len(pending) == 3
    answers = {p["args"]["qid"]: p["args"]["text"] for p in pending}
    assert answers[_qid_for(home, "T-3", "Use Postgres")] == "Postgres (matches prod)"
    assert answers[_qid_for(home, "T-3", "Cut a v2 API")] == "extend v1"
    assert answers[_qid_for(home, "T-3", "Who owns")] == "the reconciler"


def test_answer_flow_ctrl_g_accepts_all_remaining_recommendations(home):
    """AC3: Ctrl+G queues answers for every remaining question in the round that
    carries a recommendation, in one action, skipping the one that carries none
    (which stays open for a normal typed answer)."""
    _seed_round(home, "T-4", [
        ("Use Postgres or SQLite?", "Postgres (matches prod)"),
        ("Cut a v2 API or extend v1?", None),
        ("Who owns the migration script?", "the reconciler"),
    ])

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-4"
            await app.run_action("answer")
            await pilot.pause()

            await pilot.press("ctrl+g")
            await pilot.pause()
            assert app._exception is None

            # Only the no-recommendation question is left to walk through.
            assert len(app.screen_stack) == 2
            remaining_modal = app.screen_stack[-1]
            assert remaining_modal._recommend is None

            await pilot.press("e", "x", "t", "e", "n", "d", " ", "v", "1")
            await pilot.press("enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1  # walk finished

    asyncio.run(_inner())

    pending = inbox.pending(home, "T-4")
    assert len(pending) == 3
    answers = {p["args"]["qid"]: p["args"]["text"] for p in pending}
    assert answers[_qid_for(home, "T-4", "Use Postgres")] == "Postgres (matches prod)"
    assert answers[_qid_for(home, "T-4", "Cut a v2 API")] == "extend v1"
    assert answers[_qid_for(home, "T-4", "Who owns")] == "the reconciler"
