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
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("textual", reason="requires the [tui] extra (textual)")

import textual.app as _txapp  # noqa: E402
from textual.widgets import Checkbox, DataTable, Input, Select, Static, TextArea  # noqa: E402
from textual.worker import WorkerFailed  # noqa: E402

from rich.text import Text  # noqa: E402

from conftest import add_worktree, git, make_origin_and_repo, seed_phase, seed_ticket  # noqa: E402
from maestro.tui.modals import _SpecFieldsModal, _TextViewModal  # noqa: E402
from maestro import claims, config as config_mod, event_log, fleet as fleet_mod, inbox  # noqa: E402
from maestro.projection import ticket_rows  # noqa: E402
from maestro import dispatcher as disp_mod, ops as ops_mod, snapshot as snap_mod, store  # noqa: E402
from maestro.cli import main as cli_main  # noqa: E402
from maestro.statemachine import Phase  # noqa: E402
from maestro.sessions import ClaudeCliSessions, DryRunSessions, OpencodeCliSessions, PiCliSessions  # noqa: E402
from maestro.tui import (  # noqa: E402
    ReviewScreen,
    WhyScreen,
    AcScreen,
    ActivityScreen,
    _AcEvidenceModal,
    DepsScreen,
    DecisionsScreen,
    DetailScreen,
    EventsScreen,
    FleetScreen,
    InboxScreen,
    LogsScreen,
    MaestroTUI,
    ProposalScreen,
    ScheduleScreen,
    SpecScreen,
    EnvScreen,
    _AddAcModal,
    _AnswerModal,
    _CmdModal,
    _ConfirmModal,
    _CreateModal,
    _EventPayloadModal,
    _FILTERS,
    HoldModal,
    _ImportLinearModal,
    _InboxModal,
    _IntervalModal,
    _RunnerModal,
    _ScheduleModal,
    _SuggestAcsModal,
    _styled_row,
)


@pytest.fixture(autouse=True)
def _no_real_spawns(monkeypatch):
    """T-153: a human-input modal submit nudges a sweep; no test in this module
    may reach a real `claude`/`opencode`/`pi` Popen."""
    def _boom(*a, **kw):
        raise AssertionError("a real CLI backend spawn was attempted in a TUI test")
    for cls in (ClaudeCliSessions, OpencodeCliSessions, PiCliSessions):
        monkeypatch.setattr(cls, "spawn", _boom)


def _make_app(home):
    dry = DryRunSessions()
    app = MaestroTUI(home=str(home), sessions_factory=lambda cfg: dry)
    app.dry = dry
    return app


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


# --------------------------------------------------------------------------- #
# static defense: every binding resolves to a real action method              #
# --------------------------------------------------------------------------- #
# Textual SILENTLY no-ops a key bound to a missing action (run_action returns
# False, no exception) — the press-sweep above cannot see that, so guard it here.

_BINDING_CLASSES = [
    MaestroTUI, ReviewScreen, _TextViewModal, DepsScreen, DetailScreen, EventsScreen, InboxScreen, LogsScreen, FleetScreen, ProposalScreen,
    ScheduleScreen, ActivityScreen, AcScreen, DecisionsScreen, WhyScreen, _AcEvidenceModal, _AnswerModal, _CmdModal, _IntervalModal, _CreateModal, _InboxModal,
    _ScheduleModal, _RunnerModal, _ImportLinearModal, _AddAcModal, _SuggestAcsModal, _SpecFieldsModal,
    _ConfirmModal, HoldModal, SpecScreen, EnvScreen, _EventPayloadModal,
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


def test_answer_modal_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-1", "answer", _AnswerModal)  # has open questions


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
    from textual.widgets import TextArea

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


def test_detail_screen_shows_stacked_pr_entries(home):
    """T-127 AC5: a 3-entry stacked ticket's DetailScreen lists every PR in the
    stack, in order, with its own state -- proven by mounting the real app and
    pressing real keys, not by mocking query_one/push_screen."""
    seed_ticket(home, "T-1", "stacked PR ticket", phase="qa")
    for i in range(3):
        event_log.append(home, "T-1", "PrOpened", {
            "number": 200 + i, "url": f"https://example.com/pull/{200 + i}", "draft": True,
            "stack": {"index": i, "total": 3, "branch": f"maestro/T-1-{i+1}",
                      "base": "main" if i == 0 else f"maestro/T-1-{i}"},
        }, actor="r")
    snap_mod.rebuild(home, "T-1")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            assert app._selected_key == "T-1"

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            ds_text = app.screen_stack[-1].query_one("#ds-detail", Static).render().plain
            assert "#200" in ds_text and "#201" in ds_text and "#202" in ds_text
            assert "1/3" in ds_text and "2/3" in ds_text and "3/3" in ds_text
            await pilot.press("escape")
            await pilot.pause()

            assert app._exception is None

    asyncio.run(_inner())


def test_detail_pane_and_screen_render_title_from_spec_at_row_zero(home):
    """AD-7 replaces GA-18's tier-rendering test (the Tier line is gone --
    there is no more tier to render): proves both the compact #detail pane and
    the full DetailScreen (#ds-detail) still mount clean and show the ticket's
    title for the selected row."""
    seed_ticket(home, "T-1", "auto-approved change", phase="ready")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()

            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            assert app._selected_key == "T-1"
            assert "auto-approved change" in app.query_one("#detail", Static).render().plain

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            ds_text = app.screen_stack[-1].query_one("#ds-detail", Static).render().plain
            assert "auto-approved change" in ds_text
            await pilot.press("escape")
            await pilot.pause()

            assert app._exception is None

    asyncio.run(_inner())


def test_detail_pane_surfaces_provider_marker_for_a_provider_caused_degrade(home):
    """T-89 (AC5): a degraded ticket whose Failed/Stalled payload carries
    kind="provider" reads as provider-caused in the detail pane rather than a
    bare watchdog timeout -- proven by mounting the real app."""
    seed_ticket(home, "T-1", "outage casualty", phase="implementing")
    event_log.append(home, "T-1", "Failed",
                     {"error": "watchdog: no output for over 600s (pid 123)",
                      "kind": "provider", "state": "no_network"}, actor="dispatcher")
    event_log.append(home, "T-1", "Stalled",
                     {"reason": "1 failures: watchdog: no output for over 600s (pid 123)",
                      "kind": "provider", "state": "no_network"}, actor="dispatcher")
    snap_mod.rebuild(home, "T-1")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            assert app._selected_key == "T-1"
            content = app.query_one("#detail", Static).render().plain
            assert "PROVIDER" in content
            assert "no_network" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_events_screen_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-3", "view_events", EventsScreen)


def test_inbox_screen_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-3", "view_inbox", InboxScreen)


def test_inbox_screen_shows_processed_and_pending_entries(seeded_home):
    """AC1/AC2: every inbox entry lists -- both consumed and unconsumed -- with
    the pending/processed split landing exactly at the cursor, same as
    ``inbox.pending``'s own split, proved via the real mounted app."""
    inbox.append_command(seeded_home, "T-3", "msg", {"text": "already handled"})
    inbox.append_command(seeded_home, "T-3", "msg", {"text": "second processed one"})
    inbox.append_command(seeded_home, "T-3", "msg", {"text": "still pending"})
    inbox.ack(seeded_home, "T-3")  # cursor -> 2: first two processed, third pending
    inbox.append_command(seeded_home, "T-3", "retry", {})  # appended after ack -> pending

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("view_inbox")
            await pilot.pause()
            screen = app.screen_stack[-1]
            assert isinstance(screen, InboxScreen)
            from textual.widgets import RichLog
            log_widget = screen.query_one("#inbox-full", RichLog)
            content = "\n".join(strip.text for strip in log_widget.lines)
            assert "already handled" in content
            assert "second processed one" in content
            assert "still pending" in content
            assert "PROCESSED" in content
            assert "PENDING" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_inbox_screen_empty_inbox_shows_placeholder_not_crash(seeded_home):
    """AC3: a ticket with no inbox file at all -- no inbox/<KEY>.jsonl ever
    written -- shows a placeholder instead of crashing."""
    assert not store.inbox_path(seeded_home, "T-5").exists()

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("view_inbox")
            await pilot.pause()
            screen = app.screen_stack[-1]
            assert isinstance(screen, InboxScreen)
            from textual.widgets import RichLog
            log_widget = screen.query_one("#inbox-full", RichLog)
            content = "\n".join(strip.text for strip in log_widget.lines)
            assert "empty" in content.lower()
            assert app._exception is None

    asyncio.run(_inner())


def test_inbox_action_notifies_when_no_ticket_selected(seeded_home):
    """AC3: the no-selection case notifies instead of pushing a screen or crashing."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = None
            notifications_before = len(app._notifications)
            await app.run_action("view_inbox")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert len(app._notifications) > notifications_before
            assert app._exception is None

    asyncio.run(_inner())


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


def test_logs_screen_shows_third_format_log_not_blank(seeded_home):
    """AC4 (RF-3): a non-Claude ('opencode') session log renders its raw content
    in the real logs pane -- not a blank pane (the old failure mode for any
    filename the render path didn't recognize)."""
    from textual.widgets import RichLog

    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "reconcile-T-3-9999999998.000000.opencode.jsonl").write_text(
        '{"type": "message", "text": "hello from opencode"}\n', encoding="utf-8"
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
            assert rendered.strip() != ""
            assert "hello from opencode" in rendered

            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_logs_screen_renders_every_session_oldest_first_then_tails_live(seeded_home):
    """T-119: the Logs screen renders all captured sessions oldest-first under headers,
    then tails the live (claimed) one -- in the real mounted app."""
    import json as _json
    from textual.widgets import RichLog

    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)

    def _stream(word):
        return _json.dumps({"type": "assistant", "message": {"id": word, "role": "assistant",
                            "content": [{"type": "text", "text": word}]}}) + "\n"

    (log_dir / "reconcile-T-3-1000.000000.stream.jsonl").write_text(_stream("said-old"), encoding="utf-8")
    (log_dir / "reconcile-T-3-2000.000000.stream.jsonl").write_text(_stream("said-mid"), encoding="utf-8")
    live = log_dir / "reconcile-T-3-3000.000000.stream.jsonl"
    live.write_text(_stream("said-live"), encoding="utf-8")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("view_logs")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], LogsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()

            log_widget = app.screen.query_one("#logs-view", RichLog)
            rendered = "\n".join(strip.text for strip in log_widget.lines)
            assert rendered.index("said-old") < rendered.index("said-mid") < rendered.index("said-live")
            for sid in ("reconcile-T-3-1000.000000", "reconcile-T-3-2000.000000", "reconcile-T-3-3000.000000"):
                assert f"=== session {sid} | " in rendered
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_logs_screen_header_names_model(seeded_home):
    """T-120: each session's header in the mounted Logs screen names its model."""
    import json as _json
    from textual.widgets import RichLog

    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "reconcile-T-3-1000.000000.stream.jsonl").write_text(
        _json.dumps({"type": "system", "subtype": "init", "model": "claude-sonnet-5"}) + "\n", encoding="utf-8")
    (log_dir / "reconcile-T-3-2000.000000.log").write_text("plain\n", encoding="utf-8")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("view_logs")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            rendered = "\n".join(strip.text for strip in app.screen.query_one("#logs-view", RichLog).lines)
            assert "runner: claude | model: claude-sonnet-5 ===" in rendered
            assert "model: unknown ===" in rendered
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_logs_screen_renders_opencode_tool_use_and_text(seeded_home):
    """AC4 (OC-5/T-41): opencode's own verified vocabulary (step_start/tool_use/
    text/step_finish) renders as structured content in the real logs pane, not
    just a raw byte dump."""
    from textual.widgets import RichLog

    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "step_start", "part": {}},
        {"type": "text", "part": {"text": "Reading the ticket spec."}},
        {"type": "tool_use", "part": {"tool": "bash", "callID": "call_1",
                                       "state": {"input": {"command": "pytest"}}}},
        {"type": "step_finish", "part": {"reason": "stop", "cost": 0}},
    ]
    import json as _json
    (log_dir / "reconcile-T-3-9999999997.000000.opencode.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in records) + "\n", encoding="utf-8"
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
            assert "Reading the ticket spec." in rendered
            assert "bash" in rendered

            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_logs_screen_renders_a_real_pi_log_without_exception(seeded_home):
    """AC11 (T-58): a `.pi.jsonl` log -- the real captured fixture, not a
    hand-authored one -- renders under a real `run_test()` mount without
    raising, and its tool-call/text content shows up structured in the pane
    (not just a raw byte dump)."""
    from pathlib import Path
    from textual.widgets import RichLog

    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).parent / "fixtures" / "sample.pi.jsonl"
    (log_dir / "reconcile-T-3-9999999996.000000.pi.jsonl").write_text(
        fixture.read_text(encoding="utf-8"), encoding="utf-8"
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
            assert rendered.strip() != ""
            assert "bash" in rendered
            assert "hello-pi-fixture" in rendered

            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


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
            # GA-14: the line is relabeled with the unit doctor now reports
            # (agent-equivalents, not a bare session count) -- assert the
            # label itself, not just the "Spawns/hr" prefix, so a regression
            # back to session-counting can't slip the assertion.
            assert "Spawns/hr (agent-equiv)" in content
            assert "Runaway" in content
            assert "Spawn floor" in content
            # GA-11: added beside the spawn-rate line, not folded into it --
            # GA-14 rebases onto a panel that already has a spend line.
            assert "Spend today" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_shows_disabled_spawn_floor_distinguishably(seeded_home):
    """GA-8: the effective spawn floor renders beside Spawns/hr / Runaway, and a
    disabled (0) floor reads as an explicit disabled state, not a bare '0'."""
    (seeded_home / "config.toml").write_text("[maestro]\nmin_spawn_interval = 0\n")

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
            assert "Spawn floor" in content
            assert "disabled" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_shows_spend_unavailable_for_text_format(seeded_home):
    """GA-11 trap: a session_log_format = "text" home must render spend as
    explicitly unavailable, never a silent $0.00 (a zero would be a ceiling
    that can never fire)."""
    (seeded_home / "config.toml").write_text('[maestro]\nsession_log_format = "text"\n')

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
            assert "Spend today" in content
            assert "unavailable" in content
            assert "$0.00" not in content
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_shows_no_cap_for_unset_ceiling(seeded_home):
    """RB-8: with a stream-json meter available but no daily_spend_ceiling_usd
    configured (seeded_home writes no config.toml, so the ceiling defaults to
    None), the fleet panel must read as an explicit "no cap" warning -- never a
    blank or an omitted value, matching the wording style of the `unavailable`
    branch above."""
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
            assert "Spend today" in content
            assert "no cap" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_renders_runaway_board_differently(seeded_home):
    """A board that has actually exceeded its spawn budget must render visibly
    differently (RUNAWAY, red) from the healthy seeded_home above."""
    from maestro import dispatcher as disp
    from maestro.config import Config
    from maestro.statemachine import Phase
    from test_dispatcher import _EphemeralSessions
    (seeded_home / "config.toml").write_text(
        "[maestro]\nrunaway_spawns_per_hour = 1\n")
    seed_phase(seeded_home, "R-1", Phase.IN_REVIEW)
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


def test_header_badge_shows_provider_no_network_without_opening_fleet_screen(seeded_home, monkeypatch):
    """T-89 (AC1): a provider/network outage is visible on the header badge --
    the ALWAYS-visible main board, no `F`/FleetScreen (``show=False``) needed.
    Same seed shape as ``test_fleet_screen_shows_provider_no_network_distinguishably``
    below, but never pushes FleetScreen."""
    import json as json_mod
    from maestro import health
    from maestro import store as store_mod

    key = "T-3"  # seed_ticket'd into `implementing` by the seeded_home fixture
    for epoch in (100.0, 200.0, 300.0):
        session_id = f"reconcile-{key}-{epoch:.6f}"
        path = store_mod.session_stream_path(seeded_home, key, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json_mod.dumps({"type": "system", "subtype": "init", "session_id": session_id}) + "\n" +
            json_mod.dumps({"type": "result", "subtype": "error_during_execution",
                             "is_error": True}) + "\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (False, "offline"))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause(0.1)  # let the threaded badge worker land
            badge = app.query_one("#fleet-badge", Static)
            assert "NO NETWORK" in str(badge.content)
            assert app._exception is None
            assert not any(isinstance(s, FleetScreen) for s in app.screen_stack)

    asyncio.run(_inner())


def test_header_badge_shows_no_provider_warning_on_a_healthy_board(seeded_home, monkeypatch):
    """T-89 (AC2): the healthy counterpart to the test above -- no false
    positive on a board with no error streak and a reachable probe."""
    from maestro import health

    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (True, "reachable"))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause(0.1)
            content = str(app.query_one("#fleet-badge", Static).content)
            assert "NO NETWORK" not in content
            assert "ERRORING" not in content
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_shows_provider_no_network_distinguishably(seeded_home, monkeypatch):
    """MTO-8: a fleet whose recent sessions all ended in a non-429 provider
    error, with the confirmation probe unable to reach the network, must
    render distinguishably (NO NETWORK, red) from the healthy seeded_home
    above -- proven by mounting the real app, matching the ``runaway`` test
    right above."""
    import json as json_mod
    from maestro import health, store

    key = "T-3"  # seed_ticket'd into `implementing` by the seeded_home fixture
    for epoch in (100.0, 200.0, 300.0):
        session_id = f"reconcile-{key}-{epoch:.6f}"
        path = store.session_stream_path(seeded_home, key, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json_mod.dumps({"type": "system", "subtype": "init", "session_id": session_id}) + "\n" +
            json_mod.dumps({"type": "result", "subtype": "error_during_execution",
                             "is_error": True}) + "\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (False, "offline"))

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
            assert "Provider" in content
            assert "NO NETWORK" in content
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
            for _ in range(50):  # threaded worker: poll, don't race a fixed pause
                await pilot.pause(0.1)
                if fleet.pause_state(seeded_home, store.now_epoch()) is None:
                    break
            assert fleet.pause_state(seeded_home, store.now_epoch()) is None
            assert app._exception is None

            await pilot.press("P")  # toggle_pause -> pause (now unpaused)
            for _ in range(50):  # wait for the confirm modal before answering it
                await pilot.pause(0.1)
                if not isinstance(app.screen_stack[-1], FleetScreen):
                    break
            await pilot.press("y")
            for _ in range(50):
                await pilot.pause(0.1)
                if fleet.pause_state(seeded_home, store.now_epoch()) is not None:
                    break
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
            app._refresh_now()
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
            app._refresh_now()
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
    from maestro import event_log, snapshot as snap_mod

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
        "kind": "implementation", "priority": 3,
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


# --- GA-9: title/repo round-trip through add/edit/toggle, mounted app -------

def test_schedule_add_edit_toggle_roundtrips_title_and_repo(seeded_home):
    """GA-9 mounted-app QA: drives the real ScheduleScreen through add -> edit
    -> toggle via real key presses, asserting title/repo survive each step and
    that toggling one task never strips another task's fields (blast radius).
    GA-13: these three actions now call `ops.schedule_add`/`schedule_edit`/
    `schedule_set_enabled` instead of inlining load -> mutate -> write_scheduled,
    so this same real-key-press walk is this ticket's mounted-app proof that
    the ops refactor didn't change observable behavior."""
    _write_scheduled_config(seeded_home, name="other", title="Other title", repo="beta")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("S")  # schedule_panel
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], ScheduleScreen)

            # --- add: a new titled+repo-bound task -------------------------
            await pilot.press("n")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ScheduleModal)
            modal.query_one("#sched-name", Input).value = "digest"
            modal.query_one("#sched-prompt", TextArea).text = "Summarize things"
            modal.query_one("#sched-every", Input).value = "1h"
            modal.query_one("#sched-title", Input).value = "Morning digest"
            modal.query_one("#sched-repo", Input).value = "alpha"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None

            cfg = config_mod.load(str(seeded_home))
            added = next(t for t in cfg.scheduled if t["name"] == "digest")
            assert added["title"] == "Morning digest"
            assert added["repo"] == "alpha"

            # --- edit: select the new task, change its title, keep repo ----
            screen = app.screen_stack[-1]
            table = screen.query_one("#schedule-table", DataTable)
            for row_idx in range(table.row_count):
                if str(table.get_row_at(row_idx)[0]) == "digest":
                    table.move_cursor(row=row_idx)
                    break
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ScheduleModal)
            assert modal.query_one("#sched-title", Input).value == "Morning digest"
            assert modal.query_one("#sched-repo", Input).value == "alpha"
            modal.query_one("#sched-title", Input).value = "Morning digest (edited)"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None

            cfg = config_mod.load(str(seeded_home))
            edited = next(t for t in cfg.scheduled if t["name"] == "digest")
            assert edited["title"] == "Morning digest (edited)"
            assert edited["repo"] == "alpha"  # untouched field survives the merge

            # --- toggle: flip "digest" and prove "other" is untouched -------
            screen = app.screen_stack[-1]
            table = screen.query_one("#schedule-table", DataTable)
            for row_idx in range(table.row_count):
                if str(table.get_row_at(row_idx)[0]) == "digest":
                    table.move_cursor(row=row_idx)
                    break
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    cfg = config_mod.load(str(seeded_home))
    digest = next(t for t in cfg.scheduled if t["name"] == "digest")
    other = next(t for t in cfg.scheduled if t["name"] == "other")
    assert digest["enabled"] is False
    assert digest["title"] == "Morning digest (edited)"
    assert digest["repo"] == "alpha"
    # blast radius: toggling "digest" must not strip "other"'s fields
    assert other["title"] == "Other title"
    assert other["repo"] == "beta"


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


def test_schedule_modal_add_cron_task_then_toggle_survives_fields(seeded_home):
    """GA-19 mounted-app QA: drives the real ScheduleScreen through creating a
    cron+tz task, then toggling it, via real key presses -- asserting the cron
    and tz fields survive in config.toml and app._exception stays None
    throughout. No new bindings here (the modal gains Input widgets, not new
    keybindings), so nothing to add to the binding sweep."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("S")  # schedule_panel
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], ScheduleScreen)

            # --- add: a cron+tz task, no 'every' -----------------------------
            await pilot.press("n")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ScheduleModal)
            modal.query_one("#sched-name", Input).value = "weekly-digest"
            modal.query_one("#sched-prompt", TextArea).text = "Summarize the week"
            modal.query_one("#sched-cron", Input).value = "0 9 * * 1"
            modal.query_one("#sched-tz", Input).value = "America/New_York"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None

            cfg = config_mod.load(str(seeded_home))
            added = next(t for t in cfg.scheduled if t["name"] == "weekly-digest")
            assert added["cron"] == "0 9 * * 1"
            assert added["tz"] == "America/New_York"
            assert "every" not in added

            # --- toggle: flip it, cron/tz must survive the merge -------------
            screen = app.screen_stack[-1]
            table = screen.query_one("#schedule-table", DataTable)
            for row_idx in range(table.row_count):
                if str(table.get_row_at(row_idx)[0]) == "weekly-digest":
                    table.move_cursor(row=row_idx)
                    break
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    cfg = config_mod.load(str(seeded_home))
    toggled = next(t for t in cfg.scheduled if t["name"] == "weekly-digest")
    assert toggled["enabled"] is False
    assert toggled["cron"] == "0 9 * * 1"
    assert toggled["tz"] == "America/New_York"


def test_schedule_modal_rejects_both_every_and_cron(seeded_home):
    """GA-19: the modal enforces "exactly one of every/cron" client-side --
    submitting with both filled in must notify (not crash) and leave
    config.toml untouched."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            screen = app.screen_stack[-1]
            await screen.run_action("add_task")
            await pilot.pause()
            modal = app.screen_stack[-1]
            modal.query_one("#sched-name", Input).value = "both"
            modal.query_one("#sched-prompt", TextArea).text = "P"
            modal.query_one("#sched-every", Input).value = "1h"
            modal.query_one("#sched-cron", Input).value = "0 2 * * *"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None
            assert isinstance(app.screen_stack[-1], _ScheduleModal)  # still open

    asyncio.run(_inner())
    assert config_mod.load(str(seeded_home)).scheduled == []


# --- GA-13: TUI schedule verbs route through ops.schedule_* -----------------

def test_schedule_add_duplicate_name_notifies_without_writing(seeded_home):
    """GA-13 AC: the ops-owned duplicate-name check fires through the TUI too --
    submitting the add modal with a name that already exists must notify (not
    crash) and must leave config.toml's one existing task exactly as it was."""
    original = _write_scheduled_config(seeded_home, name="digest", every="1h")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("schedule_panel")
            await pilot.pause()
            screen = app.screen_stack[-1]
            await screen.run_action("add_task")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ScheduleModal)
            modal.query_one("#sched-name", Input).value = "digest"  # duplicate
            modal.query_one("#sched-prompt", TextArea).text = "A different prompt"
            modal.query_one("#sched-every", Input).value = "6h"
            notifs_before = len(app._notifications)
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app._notifications) > notifs_before
            # the modal is gone (it always dismisses itself); the screen underneath is live
            assert app.screen_stack[-1] is screen

    asyncio.run(_inner())
    cfg = config_mod.load(str(seeded_home))
    assert cfg.scheduled == [original]  # untouched -- no second task, original fields intact


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
            await pilot.press("ctrl+s")
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
            await pilot.press("ctrl+s")
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


def test_answer_flow_ctrl_r_carries_accept_marker_typed_answer_does_not(home):
    """T-140 AC1: Ctrl+R queues an `ans` command carrying the
    `accepted_recommendation` marker; an identical TYPED answer (no Ctrl+R)
    carries none, even though its text matches the recommendation verbatim.
    The marker survives `ops.fold_inbox` onto the folded `QuestionAnswered`."""
    _seed_round(home, "T-6", [
        ("Use Postgres or SQLite?", "Postgres"),
        ("Cut a v2 API or extend v1?", "extend v1"),
    ])

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-6"
            await app.run_action("answer")
            await pilot.pause()

            # Q1 -- Ctrl+R accepts the recommendation.
            assert app.screen_stack[-1]._recommend == "Postgres"
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert app._exception is None

            # Q2 -- type the SAME text as the recommendation by hand.
            modal2 = app.screen_stack[-1]
            assert modal2._recommend == "extend v1"
            await pilot.press("e", "x", "t", "e", "n", "d", " ", "v", "1")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1  # walk finished

    asyncio.run(_inner())

    q1_qid = _qid_for(home, "T-6", "Use Postgres")
    q2_qid = _qid_for(home, "T-6", "Cut a v2 API")
    pending = {p["args"]["qid"]: p["args"] for p in inbox.pending(home, "T-6")}
    assert pending[q1_qid]["text"] == "Postgres"
    assert pending[q1_qid]["accepted_recommendation"] is True
    assert pending[q2_qid]["text"] == "extend v1"
    assert "accepted_recommendation" not in pending[q2_qid]

    ops_mod.fold_inbox(config_mod.Config(home=home), "T-6")
    answered = {e["payload"]["qid"]: e["payload"] for e in event_log.read(home, "T-6")
                if e["type"] == "QuestionAnswered"}
    assert answered[q1_qid]["accepted_recommendation"] is True
    assert "accepted_recommendation" not in answered[q2_qid]


def test_answer_flow_ctrl_g_carries_accept_marker_on_every_queued_answer(home):
    """T-140 AC1: Ctrl+G's queued answers (one per remaining recommended
    question) each carry the accept marker too -- it's the same accept action
    as Ctrl+R, just for every remaining recommendation in the round at once."""
    _seed_round(home, "T-7", [
        ("Use Postgres or SQLite?", "Postgres"),
        ("Who owns the migration script?", "the reconciler"),
    ])

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-7"
            await app.run_action("answer")
            await pilot.pause()

            await pilot.press("ctrl+g")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1  # walk finished, nothing left unanswered

    asyncio.run(_inner())

    q1_qid = _qid_for(home, "T-7", "Use Postgres")
    q2_qid = _qid_for(home, "T-7", "Who owns")
    pending = {p["args"]["qid"]: p["args"] for p in inbox.pending(home, "T-7")}
    assert pending[q1_qid]["accepted_recommendation"] is True
    assert pending[q2_qid]["accepted_recommendation"] is True


# --------------------------------------------------------------------------- #
# T-109: multi-line text in the inbox answer input                            #
# --------------------------------------------------------------------------- #

def test_answer_modal_multiline_text_reaches_inbox_verbatim(home):
    """AC1+AC2: the answer field is a TextArea whose Enter inserts a newline
    (never submits) and whose dedicated Ctrl+S submits; the full multi-line
    text reaches the ticket's inbox with the newline preserved."""
    _seed_round(home, "T-5", [("Describe the steps", None)])

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("answer")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _AnswerModal)
            textarea = modal.query_one("#answer-input", TextArea)
            textarea.focus()
            await pilot.pause()
            await pilot.press("s", "t", "e", "p", " ", "1")
            await pilot.press("enter")  # newline inside TextArea, must not submit
            await pilot.pause()
            assert app.screen_stack[-1] is modal, "Enter must insert a newline, not submit"
            await pilot.press("s", "t", "e", "p", " ", "2")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1, "modal should have dismissed after Ctrl+S"

    asyncio.run(_inner())

    pending = inbox.pending(home, "T-5")
    assert len(pending) == 1
    assert pending[0]["args"]["text"] == "step 1\nstep 2"


def test_answer_modal_escape_cancels_without_writing(home):
    """AC2: Esc still cancels the answer modal without writing anything to the
    inbox, even with unsent typed text sitting in the field."""
    _seed_round(home, "T-6", [("Pick an approach", None)])

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-6"
            await app.run_action("answer")
            await pilot.pause()
            modal = app.screen_stack[-1]
            textarea = modal.query_one("#answer-input", TextArea)
            textarea.focus()
            await pilot.pause()
            await pilot.press("d", "r", "a", "f", "t")
            await pilot.press("escape")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())

    assert inbox.pending(home, "T-6") == []


def test_answer_modal_single_line_answer_byte_identical(home):
    """AC3: a single-line answer submitted via the new TextArea-based modal
    reaches the inbox exactly as it did through the old Input -- no trailing
    newline, no markup regression."""
    _seed_round(home, "T-7", [("Postgres or SQLite?", None)])

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-7"
            await app.run_action("answer")
            await pilot.pause()
            modal = app.screen_stack[-1]
            textarea = modal.query_one("#answer-input", TextArea)
            textarea.focus()
            await pilot.pause()
            await pilot.press("p", "o", "s", "t", "g", "r", "e", "s")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())

    pending = inbox.pending(home, "T-7")
    assert len(pending) == 1
    assert pending[0]["args"]["text"] == "postgres"


def test_answer_modal_submit_button_clicks_submit(home):
    """Human follow-up on T-109: a visible Submit button next to the TextArea
    submits the answer the same as Ctrl+S, for anyone typing in a terminal
    where Ctrl+S isn't deliverable."""
    _seed_round(home, "T-8", [("Postgres or SQLite?", None)])

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-8"
            await app.run_action("answer")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _AnswerModal)
            textarea = modal.query_one("#answer-input", TextArea)
            textarea.focus()
            await pilot.pause()
            await pilot.press("p", "o", "s", "t", "g", "r", "e", "s")
            await pilot.click("#answer-submit-button")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1, "modal should have dismissed after clicking Submit"

    asyncio.run(_inner())

    pending = inbox.pending(home, "T-8")
    assert len(pending) == 1
    assert pending[0]["args"]["text"] == "postgres"


def test_detail_pane_and_screen_render_title_from_spec(home):
    """A ticket whose log carries no TicketCreated folds to `title = None`, but
    its spec's H1 has the title -- both the compact #detail pane and the full
    DetailScreen (#ds-detail) must show it (`snapshot.display_title`), and the
    normally-minted case must keep rendering the folded title."""
    # no TicketCreated: exactly what a directory-discovered / already-advanced key looks like
    store.atomic_write(store.spec_path(home, "X-1"),
                       "# X-1: title only in the spec\n\napproval_tier: 1\n\n## Intent\nb\n")
    event_log.append(home, "X-1", "SpecObserved", {"spec_hash": "abc"}, actor="r")
    event_log.append(home, "X-1", "PhaseChanged", {"phase": "ready", "reason": ""}, actor="r")
    snap_mod.rebuild(home, "X-1")
    seed_ticket(home, "X-2", "folded title wins", phase="ready")

    def _title_line(static: Static) -> str:
        return static.render().plain.splitlines()[0]

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()

            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            assert app._selected_key == "X-1"
            assert "title only in the spec" in _title_line(app.query_one("#detail", Static))

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DetailScreen)
            assert "title only in the spec" in _title_line(
                app.screen_stack[-1].query_one("#ds-detail", Static))
            await pilot.press("escape")
            await pilot.pause()

            # the folded title still wins for a normally-minted ticket
            table.move_cursor(row=1)
            await pilot.pause()
            assert app._selected_key == "X-2"
            assert "folded title wins" in _title_line(app.query_one("#detail", Static))

            # and the board row itself carries it
            titles = {str(table.get_row_at(r)[0]): str(table.get_row_at(r)[2])
                      for r in range(table.row_count)}
            assert "title only in the spec" in titles["X-1"]

            assert app._exception is None

    asyncio.run(_inner())


def test_compact_and_project_rebuild_do_not_race_on_shared_derived_files(seeded_home):
    """RB-5: `ops.compact` (`action_compact`) and the projection rebuild
    (`action_project_rebuild`) both run on Textual worker *threads* inside
    this one process and both write under `derived/*` -- the exact shape the
    bug was in (`store.atomic_write`'s old pid-only temp name collided across
    threads of one process, surfacing as a bare `OSError`/`FileNotFoundError`
    escaping into a Textual callback). Confirming the compact modal and then
    immediately triggering the rebuild -- before waiting for either worker --
    lets Textual actually run both concurrently; `app._exception is None`
    proves neither raced the other into a torn/missing temp file."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"

            await app.run_action("compact")  # opens the y/n confirm modal
            await pilot.pause()
            await pilot.press("y")  # confirm -> spawns the compact worker thread
            await app.run_action("project_rebuild")  # spawns the rebuild worker thread
            await pilot.pause()

            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())

    # Both writers actually landed -- not just "didn't crash".
    assert store.events_archive_path(seeded_home, "T-3").exists()
    dashboards = list((seeded_home / "derived").glob("*.md"))
    assert dashboards, "projection rebuild did not write any dashboard"


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


# --------------------------------------------------------------------------- #
# T-112: 'A' -> _AddAcModal -> ops.add_ac                                     #
# --------------------------------------------------------------------------- #

def test_add_ac_action_notifies_when_no_ticket_selected(seeded_home):
    """The `key is None` guard the binding sweep exercises for every key: pressing
    'A' with nothing selected notifies instead of pushing a screen."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = None
            notifications_before = len(app._notifications)
            await app.run_action("add_ac")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert len(app._notifications) > notifications_before
            assert app._exception is None

    asyncio.run(_inner())


def test_add_ac_modal_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-5", "add_ac", _AddAcModal)


def test_add_ac_modal_round_trip_appends_ac_to_spec(seeded_home):
    """AC2: typing text and confirming writes the new AC to spec.md via
    `ops.add_ac`, and notifies."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            notifications_before = len(app._notifications)
            await app.run_action("add_ac")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _AddAcModal)
            modal.query_one("#add-ac-input", Input).value = "a new thing works"
            await pilot.press("enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1
            assert len(app._notifications) > notifications_before

    asyncio.run(_inner())
    after = store.spec_path(seeded_home, "T-5").read_text()
    assert snap_mod.parse_acs(after) == ["ok", "a new thing works"]


def test_add_ac_modal_cancel_writes_nothing(seeded_home):
    """Cancelling (Esc) writes nothing -- spec bytes on disk are unchanged."""
    before_bytes = store.spec_path(seeded_home, "T-5").read_bytes()

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("add_ac")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _AddAcModal)
            modal.query_one("#add-ac-input", Input).value = "should never land"
            await pilot.press("escape")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())
    assert store.spec_path(seeded_home, "T-5").read_bytes() == before_bytes


def test_add_ac_modal_blank_text_dismisses_without_writing(seeded_home):
    """Submitting blank/whitespace-only text is treated the same as cancel --
    the modal's own `_submit` dismisses with `None` rather than calling
    `ops.add_ac` with nothing to add."""
    before_bytes = store.spec_path(seeded_home, "T-5").read_bytes()

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await app.run_action("add_ac")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _AddAcModal)
            await pilot.press("enter")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())
    assert store.spec_path(seeded_home, "T-5").read_bytes() == before_bytes


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


# --------------------------------------------------------------------------- #
# T-117: "Q" -> action_trigger_post_qa -> dispatcher.trigger_post_qa_skill    #
# `trigger_post_qa_skill` is mocked as the external boundary (a real success #
# ultimately spawns a session, `ops_mod.suggest_acs` above is the same       #
# shape) -- these tests exist to catch a regression where the action built   #
# `Config` directly instead of `config_mod.load()`, silently ignoring       #
# `config.toml` and making every manual fire report "not configured"        #
# regardless of what the board actually has set.                            #
# --------------------------------------------------------------------------- #

def test_trigger_post_qa_action_notifies_when_no_ticket_selected(seeded_home):
    """The `key is None` guard the binding sweep exercises for every key."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = None
            notifications_before = len(app._notifications)
            await app.run_action("trigger_post_qa")
            await pilot.pause()
            assert len(app._notifications) > notifications_before
            assert app._exception is None

    asyncio.run(_inner())


def test_trigger_post_qa_action_loads_real_config_toml(seeded_home, monkeypatch):
    """Regression test for the `Config(home=...)` bug: with `post_qa_skill`
    genuinely set in `config.toml`, the `cfg` the action hands to
    `trigger_post_qa_skill` must carry that value -- a bare `Config` dataclass
    would resolve it to `None` and the ticket's repo binding would never see
    it, no matter what's on disk."""
    (seeded_home / "config.toml").write_text(
        '[maestro]\npost_qa_skill = "/post-qa-polish"\npost_qa_skill_runner = "pi"\n',
        encoding="utf-8")
    captured = {}

    def _fake_trigger(cfg, sessions, key, **kw):
        captured["post_qa_skill"] = cfg.post_qa_skill
        captured["key"] = key
        return {"key": key, "skill": cfg.post_qa_skill, "runner": "pi", "pid": 1}
    monkeypatch.setattr(disp_mod, "trigger_post_qa_skill", _fake_trigger)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await app.run_action("trigger_post_qa")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    assert captured == {"post_qa_skill": "/post-qa-polish", "key": "T-1"}


def test_trigger_post_qa_action_real_not_configured_error_notify(seeded_home):
    """End-to-end (no mocking of `trigger_post_qa_skill`): with no
    `post_qa_skill` on the board, the real dispatcher call raises
    `MaestroError` and the app surfaces it via notify instead of crashing --
    same posture as the "suggest-acs" error path."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            notifications_before = len(app._notifications)
            await app.run_action("trigger_post_qa")
            with pytest.raises(WorkerFailed):
                await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._exception is None
            assert len(app._notifications) > notifications_before
            assert "no post_qa_skill configured" in list(app._notifications)[-1].message

    asyncio.run(_inner())


def test_fleet_up_worker_loads_config_and_passes_it_through(seeded_home, monkeypatch):
    """`fleet up` from the TUI must reach `fleet.up` with a loaded Config.

    The call happens inside a `run_worker` thread lambda, where a raise is a
    silent no-op at runtime -- nothing surfaces, the fleet just never comes up.
    So this drives the real binding through the real worker and asserts the
    kwargs that arrived. `fleet_mod.up` itself is the one mocked boundary: it
    shells `install.sh` and `launchctl`, which a test must never do.
    """
    seen = {}

    def _fake_up(home, interval=300, **kw):
        seen["home"] = home
        seen["interval"] = interval
        seen["cfg"] = kw.get("cfg")
        return {"action": "up", "interval": interval, "rc": 0, "stdout": "", "label": "x"}

    monkeypatch.setattr(fleet_mod, "up", _fake_up)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await pilot.press("u")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _IntervalModal)
            await pilot.press("2", "0", "0", "enter")   # submit a real interval
            await pilot.pause()
            for _ in range(20):
                if "cfg" in seen:
                    break
                await pilot.pause(0.05)
            assert app._exception is None

    asyncio.run(_inner())

    assert seen.get("interval") == 200
    cfg = seen.get("cfg")
    assert cfg is not None, "fleet.up was called without a Config"
    from maestro.config import Config
    assert isinstance(cfg, Config)
    # The whole point: the runner dirs it needs come off this Config.
    from maestro import fleet as _f
    assert isinstance(_f.config_runner_dirs(cfg), list)


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


def test_no_select_blank_sentinel_in_tui():
    """Textual 8: Select.BLANK is Widget.BLANK (False), not the no-selection sentinel."""
    from pathlib import Path

    import maestro

    offenders = [str(p) for p in Path(maestro.__file__).parent.rglob("*.py")
                 if "Select.BLANK" in p.read_text()]
    assert offenders == []


def test_select_kind_values_survive_null_sentinel_switch(seeded_home):
    async def _inner():
        import json
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

# --- T-174: #pulse strip ---------------------------------------------------

def _pulse_cfg(home, body="", spend_total=None):
    (home / "config.toml").write_text("[maestro]\n" + body, encoding="utf-8")
    if spend_total is not None:
        store.write_json(home / "derived" / ".spend.json",
                         {"date": store.utc_date(store.now_epoch()), "total_usd": spend_total})


def _pulse_ledger(home, key, n, weight=1):
    now = store.now_epoch()
    store.write_json(disp_mod._spawn_ledger_path(home),
                     {key: {"recent": [[now - 10 - i, weight] for i in range(n)]}})


async def _pulse_tick(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    app._refresh_pulse()
    await app.workers.wait_for_complete()
    await pilot.pause()
    return str(app.query_one("#pulse", Static).content)


@pytest.fixture
def no_probe(monkeypatch):
    from maestro import health
    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (True, "reachable"))


def test_pulse_strip_renders_seeded_state(seeded_home, no_probe):
    _pulse_cfg(seeded_home, "daily_spend_ceiling_usd = 10.0\nrunaway_spawns_per_hour = 50\n", 2.5)
    _pulse_ledger(seeded_home, "T-3", 7)
    store.write_json(store.heartbeat_path(seeded_home),
                     {"active": 3, "due": 5, "throttled": 2, "spawned": 0})

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            text = await _pulse_tick(app, pilot)
            assert "spawns 7/50 /hr" in text
            assert "$2.50 / $10.00" in text
            assert "3·5·2" in text
            assert "rate-limited" not in text
            assert app._exception is None

    asyncio.run(_inner())


def test_pulse_strip_spend_thresholds(seeded_home, no_probe):
    async def _text(body, total):
        _pulse_cfg(seeded_home, body, total)
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            text = await _pulse_tick(app, pilot)
            assert app._exception is None
            return text

    async def _inner():
        cap = "daily_spend_ceiling_usd = 10.0\n"
        low = await _text(cap, 1.0)
        assert "$1.00 / $10.00" in low and "[yellow]$1.00" not in low and "[red bold]$1.00" not in low
        warn = await _text(cap, 5.0)
        assert "[yellow]$5.00[/yellow] / $10.00" in warn
        over = await _text(cap, 10.0)
        assert "[red bold]$10.00[/red bold] / $10.00" in over
        assert "no cap" in await _text("", 1.0)
        assert "unavailable" in await _text('session_log_format = "text"\n', None)

    asyncio.run(_inner())


def test_pulse_strip_rate_limit_segment(seeded_home, no_probe):
    _pulse_cfg(seeded_home)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            assert "rate-limited" not in await _pulse_tick(app, pilot)
            until = store.now_epoch() + 1800
            store.write_json(seeded_home / "derived" / ".ratelimit.json", {"paused_until": until})
            text = await _pulse_tick(app, pilot)
            from datetime import datetime, timezone
            hhmm = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%H:%M")
            assert f"rate-limited → {hhmm}" in text
            store.write_json(seeded_home / "derived" / ".ratelimit.json",
                             {"paused_until": store.now_epoch() - 5})
            assert "rate-limited" not in await _pulse_tick(app, pilot)
            assert app._exception is None

    asyncio.run(_inner())


def test_pulse_config_error_shows_short_message(seeded_home, no_probe):
    (seeded_home / "config.toml").write_text("[maestro]\nbogus_key = 1\n", encoding="utf-8")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            assert "pulse: config error" in await _pulse_tick(app, pilot)
            assert app._exception is None

    asyncio.run(_inner())


def test_pulse_toast_once_per_rising_edge(seeded_home, no_probe):
    _pulse_cfg(seeded_home, "runaway_spawns_per_hour = 4\n")

    async def _inner():
        app = _make_app(seeded_home)
        toasts = []
        app.notify = lambda msg, **kw: toasts.append(msg)
        async with app.run_test(size=(160, 40)) as pilot:
            await _pulse_tick(app, pilot)
            assert toasts == []
            _pulse_ledger(seeded_home, "T-3", 9)
            for _ in range(3):
                await _pulse_tick(app, pilot)
            assert len(toasts) == 1 and "T-3" in toasts[0] and "F" in toasts[0]
            _pulse_ledger(seeded_home, "T-3", 1)
            await _pulse_tick(app, pilot)
            assert len(toasts) == 1
            _pulse_ledger(seeded_home, "T-4", 9)
            await _pulse_tick(app, pilot)
            await _pulse_tick(app, pilot)
            assert len(toasts) == 2 and "T-4" in toasts[1]
            assert app._exception is None

    asyncio.run(_inner())


def test_pulse_strip_is_read_only(seeded_home, no_probe):
    _pulse_cfg(seeded_home, "daily_spend_ceiling_usd = 10.0\nrunaway_spawns_per_hour = 2\n")
    _pulse_ledger(seeded_home, "T-3", 5)
    store.write_json(seeded_home / "derived" / ".ratelimit.json",
                     {"paused_until": store.now_epoch() + 600})

    def _snapshot():
        return {p.relative_to(seeded_home): p.read_bytes()
                for p in (seeded_home / "derived").rglob("*")
                if p.is_file() and p.name != ".provider_probe_cache.json"}

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            await _pulse_tick(app, pilot)
            before = _snapshot()
            for _ in range(3):
                await _pulse_tick(app, pilot)
            assert _snapshot() == before
            names = {p.name for p in before}
            assert ".spend.json" not in names and ".alarm.json" not in names
            assert app._exception is None

    asyncio.run(_inner())


def test_pulse_strip_does_not_collapse_filter_bar(seeded_home, no_probe):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _pulse_tick(app, pilot)
            bar = app.query_one("#filter-bar", Static)
            pulse = app.query_one("#pulse", Static)
            header = app.query_one("Header")
            assert bar.region.height == 1 and pulse.region.height == 1
            assert bar.region.y != pulse.region.y
            assert min(bar.region.y, pulse.region.y) >= header.region.y + header.region.height
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# T-153: human input nudges a key-scoped sweep and toasts the outcome          #
# --------------------------------------------------------------------------- #

def _capture_toasts(app):
    toasts: list[str] = []
    real = app.notify

    def _notify(message, *a, **kw):
        toasts.append(str(message))
        return real(message, *a, **kw)

    app.notify = _notify
    return toasts


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_tui_answer_nudges_key_scoped_sweep(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            toasts = _capture_toasts(app)
            app._selected_key = "T-1"
            await app.run_action("answer")
            await pilot.pause()
            for _ in range(2):  # two open questions
                await pilot.press("a", "n", "s")
                await pilot.press("ctrl+s")
                await pilot.pause()
            await _settle(app, pilot)
            assert [s[0] for s in app.dry.spawned] == ["T-1"]
            assert any("T-1: spawned" in t for t in toasts), toasts
            assert app._exception is None

    asyncio.run(_inner())


def test_tui_nudge_respects_config_off(seeded_home):
    (seeded_home / "config.toml").write_text(
        "[maestro]\nnudge_on_human_input = false\n", encoding="utf-8")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await app.run_action("answer")
            await pilot.pause()
            for _ in range(2):
                await pilot.press("a", "n", "s")
                await pilot.press("ctrl+s")
                await pilot.pause()
            await _settle(app, pilot)
            assert app.dry.spawned == []
            assert len(inbox.pending(seeded_home, "T-1")) == 2
            assert app._exception is None

    asyncio.run(_inner())


def test_tui_nudge_reports_fleet_paused(seeded_home):
    fleet_mod.pause(seeded_home, reason="test")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            toasts = _capture_toasts(app)
            app._selected_key = "T-2"
            await pilot.press("ctrl+r")
            await _settle(app, pilot)
            assert app.dry.spawned == []
            assert any("T-2" in t and "paused" in t for t in toasts), toasts
            assert app._exception is None

    asyncio.run(_inner())


def test_tui_create_nudge_mints_key(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            toasts = _capture_toasts(app)
            await app.run_action("create")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _CreateModal)
            modal.query_one("#create-title", Input).value = "Brand new thing"
            modal.query_one("#create-prefix", Select).value = "T"
            await pilot.pause()
            await pilot.press("ctrl+enter")
            await _settle(app, pilot)
            assert app._exception is None
        minted = [t for t in toasts if t.startswith("minted ")]
        assert minted and "T-6" in minted[0], toasts
        assert store.spec_path(seeded_home, "T-6").exists()

    asyncio.run(_inner())


def test_tui_nudge_toggle_off(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _settle(app, pilot)
            await pilot.press("N")
            await pilot.pause()
            assert "nudge:off" in str(app.query_one("#fleet-badge", Static).render())
            app._selected_key = "T-3"
            await app.run_action("inbox_message")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _InboxModal)
            await pilot.press("h", "i", "enter")
            await _settle(app, pilot)
            assert app.dry.spawned == []
            assert len(inbox.pending(seeded_home, "T-3")) == 1
            assert app._exception is None

    asyncio.run(_inner())


# T-171: LogsScreen live console                                              #
# --------------------------------------------------------------------------- #

def _console_stream(path, *, extra=()):
    import json as _json
    recs = [{"type": "system", "subtype": "init", "model": "m-x"}]
    for out in (1, 2, 5):  # one message split over three records
        recs.append({"type": "assistant", "message": {
            "id": "m1", "usage": {"input_tokens": 10, "output_tokens": out},
            "content": [{"type": "text", "text": f"said-{path.name.split('-')[3][:4]}"}]}})
    recs += list(extra)
    recs += [{"type": "result", "subtype": "success", "total_cost_usd": 0.40, "num_turns": 22},
             {"type": "result", "subtype": "success", "total_cost_usd": 0.84, "num_turns": 22,
              "duration_ms": 1000}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_json.dumps(r) + "\n" for r in recs), encoding="utf-8")


async def _open_logs(app, pilot):
    await pilot.pause()
    app._selected_key = "T-3"
    await app.run_action("view_logs")
    await pilot.pause()
    assert isinstance(app.screen_stack[-1], LogsScreen)
    await app.workers.wait_for_complete()
    await pilot.pause()


def _pane(app):
    from textual.widgets import RichLog
    return "\n".join(s.text for s in app.screen.query_one("#logs-view", RichLog).lines)


def test_logs_console_header_shows_cost_and_turns(seeded_home):
    _console_stream(seeded_home / "agent-logs" / "T-3" / "reconcile-T-3-3000.000000.stream.jsonl")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_logs(app, pilot)
            hdr = str(app.screen.query_one("#logs-summary", Static).render())
            assert "$0.84" in hdr and "22 turns" in hdr
            assert app._exception is None

    asyncio.run(_inner())


def test_logs_console_follow_toggle(seeded_home):
    _console_stream(seeded_home / "agent-logs" / "T-3" / "reconcile-T-3-3000.000000.stream.jsonl")

    async def _inner():
        from textual.widgets import RichLog
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_logs(app, pilot)
            idx = app._filter_idx
            log = app.screen.query_one("#logs-view", RichLog)
            await pilot.press("f")
            await pilot.pause()
            assert log.auto_scroll is False
            assert "paused" in str(app.screen.query_one("#logs-summary", Static).render())
            await pilot.press("f")
            await pilot.pause()
            assert log.auto_scroll is True
            assert "paused" not in str(app.screen.query_one("#logs-summary", Static).render())
            assert app._filter_idx == idx and app._exception is None

    asyncio.run(_inner())


def test_logs_console_session_picker(seeded_home):
    d = seeded_home / "agent-logs" / "T-3"
    _console_stream(d / "reconcile-T-3-1000.000000.stream.jsonl")
    _console_stream(d / "reconcile-T-3-2000.000000.stream.jsonl")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_logs(app, pilot)
            assert "said-1000" in _pane(app)
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("down", "enter")  # newest first -> second is the older
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            pane = _pane(app)
            assert "reconcile-T-3-1000.000000" in pane
            assert "reconcile-T-3-2000.000000" not in pane
            running = [w for w in app.workers if w.name == "tail-logs" and w.is_running]
            assert len(running) <= 1
            assert app._exception is None

    asyncio.run(_inner())


def test_logs_console_auto_advances_on_claim_swap(seeded_home):
    d = seeded_home / "agent-logs" / "T-3"
    a, b = d / "reconcile-T-3-1000.000000.stream.jsonl", d / "reconcile-T-3-2000.000000.stream.jsonl"
    _console_stream(a)
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    p1, p2 = subprocess.Popen(sleeper), subprocess.Popen(sleeper)

    def _claim(proc, path):
        store.write_json(claims.claim_path(seeded_home, "T-3"),
                         {"pid": proc.pid, "name": "reconcile-T-3", "ts": store.iso_now(),
                          "epoch": store.now_epoch(), "log_path": str(path)})

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _claim(p1, a)
            app._selected_key = "T-3"
            await app.run_action("view_logs")
            await pilot.pause(1.0)
            p1.kill()
            p1.wait()
            _console_stream(b)
            _claim(p2, b)
            for _ in range(40):
                await pilot.pause(0.25)
                if "said-2000" in _pane(app):
                    break
            pane = _pane(app)
            assert "── next session" in pane
            assert pane.index("── next session") < pane.rindex("said-2000")
            assert claims.claim_path(seeded_home, "T-3").exists()
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()

    try:
        asyncio.run(_inner())
    finally:
        for p in (p1, p2):
            p.kill()
            p.wait(timeout=5)


def test_logs_console_jump_to_next_error(seeded_home):
    import json as _json
    from textual.widgets import RichLog
    path = seeded_home / "agent-logs" / "T-3" / "reconcile-T-3-3000.000000.stream.jsonl"
    recs = []
    for i in range(80):
        recs.append({"type": "assistant", "message": {"id": f"m{i}", "content": [
            {"type": "text", "text": f"filler-{i}"}]}})
        if i in (30, 60):
            recs.append({"type": "result", "subtype": "success", "is_error": True, "result": "boom"})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_json.dumps(r) + "\n" for r in recs), encoding="utf-8")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 20)) as pilot:
            await _open_logs(app, pilot)
            log = app.screen.query_one("#logs-view", RichLog)
            assert len(app.screen._err_lines) == 2
            log.scroll_to(y=0, animate=False)
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            first = log.scroll_y
            assert first == app.screen._err_lines[0]
            await pilot.press("e")
            await pilot.pause()
            assert log.scroll_y == app.screen._err_lines[1] > first
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# T-154: default-No confirms, typed-key discard, release refuses live claims  #
# --------------------------------------------------------------------------- #

def test_confirm_modal_defaults_to_no(seeded_home):
    """Enter on the (Cancel-focused) confirm cancels; only `y` compacts."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("compact")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _ConfirmModal)
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not isinstance(app.screen_stack[-1], _ConfirmModal)
            assert not store.events_archive_path(seeded_home, "T-3").exists()

            await app.run_action("compact")
            await pilot.pause()
            await pilot.press("y")
            for _ in range(50):  # threaded worker: poll, don't race a fixed pause
                await pilot.pause(0.1)
                if store.events_archive_path(seeded_home, "T-3").exists():
                    break
            assert store.events_archive_path(seeded_home, "T-3").exists()
            assert app._exception is None

    asyncio.run(_inner())


def test_discard_requires_typed_key(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-2"
            await pilot.press("ctrl+d")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ConfirmModal)
            await pilot.press("enter")
            await pilot.pause()
            assert inbox.pending(seeded_home, "T-2") == []

            await pilot.press("ctrl+d")
            await pilot.pause()
            modal = app.screen_stack[-1]
            confirm = modal.query_one("#confirm-ok")
            assert confirm.disabled
            modal.query_one("#confirm-input", Input).value = "T-3"
            await pilot.pause()
            assert confirm.disabled
            modal.query_one("#confirm-input", Input).value = "T-2"
            await pilot.pause()
            assert not confirm.disabled
            confirm.press()
            await pilot.pause()
            pending = inbox.pending(seeded_home, "T-2")
            assert [c.get("command", c.get("cmd")) for c in pending] == ["discard"]
            assert app._exception is None

    asyncio.run(_inner())


def test_cmd_discard_requires_typed_key(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-2"
            await app.run_action("cmd")
            await pilot.pause()
            cmd_modal = app.screen_stack[-1]
            assert isinstance(cmd_modal, _CmdModal)
            cmd_modal.dismiss(("discard", ""))
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ConfirmModal)
            await pilot.press("escape")
            await pilot.pause()
            assert inbox.pending(seeded_home, "T-2") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_down_and_pause_confirm(seeded_home, monkeypatch):
    from maestro import fleet
    calls = []
    monkeypatch.setattr("maestro.fleet.down", lambda *a, **kw: calls.append(a))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause(0.2)
            assert isinstance(app.screen_stack[-1], FleetScreen)

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("n")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == []

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("y")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(calls) == 1

            assert fleet.pause_state(seeded_home, store.now_epoch()) is None
            await pilot.press("P")
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert fleet.pause_state(seeded_home, store.now_epoch()) is None
            assert app._exception is None

    asyncio.run(_inner())


def test_release_refuses_live_claim(seeded_home):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        claims.write_claim(seeded_home, "T-3", proc.pid, "reconcile-T-3")
        claim_file = seeded_home / "derived" / "claims" / "T-3.json"

        async def _live():
            app = _make_app(seeded_home)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app._selected_key = "T-3"
                await pilot.press("z")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert not isinstance(app.screen_stack[-1], _ConfirmModal)
                msgs = [n.message for n in app._notifications]
                assert any("live" in m and str(proc.pid) in m for m in msgs)
                assert claim_file.exists() and proc.poll() is None
                assert app._exception is None

        asyncio.run(_live())
    finally:
        proc.kill()
        proc.wait()

    async def _stale():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await pilot.press("z")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _ConfirmModal)
            await pilot.press("y")
            await pilot.pause()
            assert not claim_file.exists()
            assert app._exception is None

    asyncio.run(_stale())


def _cmd_notifs(app):
    return [n.message for n in app._notifications]


def _submit_cmd(app, key, command):
    app._selected_key = key
    return app.run_action("cmd")


def test_cmd_refuses_qidless_answer_with_multiple_open_questions(home):
    seed_ticket(home, "T-1", "two questions", phase="awaiting-human",
                questions={"q1": "a?", "q2": "b?"})

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_cmd(app, "T-1", "approve")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _CmdModal)
            modal.query_one("#cmd-input", Input).value = "approve"
            modal._submit()
            await pilot.pause()
            msgs = [m for m in _cmd_notifs(app) if "T-1" in m and "2" in m]
            assert msgs
            assert not store.inbox_path(home, "T-1").exists() or not store.inbox_path(home, "T-1").read_text().strip()
            assert inbox.pending(home, "T-1") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_cmd_answer_single_open_question_still_queues(home):
    seed_ticket(home, "T-1", "one question", phase="awaiting-human",
                questions={"q1": "a?"})

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_cmd(app, "T-1", "approve")
            await pilot.pause()
            modal = app.screen_stack[-1]
            modal.query_one("#cmd-input", Input).value = "approve"
            modal._submit()
            await pilot.pause()
            pending = inbox.pending(home, "T-1")
            assert [c.get("command", c.get("cmd")) for c in pending] == ["approve"]
            assert not [m for m in _cmd_notifs(app) if "open questions" in m]
            assert app._exception is None

    asyncio.run(_inner())


def test_cmd_modal_has_no_requeue_row(home):
    from maestro.tui import _PHASE_COMMANDS, _DEFAULT_COMMANDS
    seed_ticket(home, "T-4", "ready ticket", phase="ready")
    for cmd, desc in _DEFAULT_COMMANDS + [r for rows in _PHASE_COMMANDS.values() for r in rows]:
        assert "requeue" not in cmd and "requeue" not in desc

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(_CmdModal("T-4", "ready"), lambda _: None)
            await pilot.pause()
            rows = [str(lbl.content) for lbl in app.screen.query("Label.cmd-row")]
            assert rows and not any("requeue" in r for r in rows)
            assert app._exception is None

    asyncio.run(_inner())


def test_cmd_modal_rows_match_real_routing():
    from maestro.tui import _PHASE_COMMANDS, _DEFAULT_COMMANDS
    all_rows = _DEFAULT_COMMANDS + [r for rows in _PHASE_COMMANDS.values() for r in rows]
    retries = [r for r in all_rows if r[0] == "retry"]
    assert retries and all(desc == "re-enter ready" for _c, desc in retries)
    assert not any("<qid>" in c or "<qid>" in d for c, d in all_rows)


def test_cmd_discard_not_refused_with_multiple_open_questions(home):
    seed_ticket(home, "T-1", "two questions", phase="awaiting-human",
                questions={"q1": "a?", "q2": "b?"})

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_cmd(app, "T-1", "discard")
            await pilot.pause()
            app.screen_stack[-1].dismiss(("discard", ""))
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _ConfirmModal)
            modal.query_one("#confirm-input", Input).value = "T-1"
            await pilot.pause()
            modal.query_one("#confirm-ok").press()
            await pilot.pause()
            pending = inbox.pending(home, "T-1")
            assert [c.get("command", c.get("cmd")) for c in pending] == ["discard"]
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# T-161: sortable board columns + Idle column                                 #
# --------------------------------------------------------------------------- #

def _seed_sort_board(home):
    for i in (1, 2, 3, 4, 5):
        seed_phase(home, f"T-{i}", disp_mod.Phase.READY)
    for n in range(3):
        event_log.append(home, "T-3", "Failed", {"error": f"boom {n}"}, actor="r")
    snap_mod.rebuild(home, "T-3")


def _cursor_key(app):
    table = app.query_one("#tickets", DataTable)
    return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value


def _row_values(app):
    table = app.query_one("#tickets", DataTable)
    return [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]


def _bar(app):
    return str(app.query_one("#filter-bar").render())


def test_idle_column_shows_time_since_last_event(home, monkeypatch):
    real = store.iso_now
    old = time.time() - 2 * 3600 - 30
    monkeypatch.setattr(store, "iso_now",
                        lambda: datetime.fromtimestamp(old, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    seed_phase(home, "T-1", disp_mod.Phase.READY)
    monkeypatch.setattr(store, "iso_now", real)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            labels = [c.label.plain for c in table.columns.values()]
            assert labels[labels.index("Deps") + 1] == "Idle"
            assert table.get_row("T-1")[labels.index("Idle")] == "2h"
            assert app._exception is None

    asyncio.run(_inner())


# T-155: ticket actions target the visible ticket                              #
# --------------------------------------------------------------------------- #

def _spy_notify(app) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    real = app.notify

    def _notify(message, *a, severity="information", **kw):
        seen.append((str(message), severity))
        return real(message, *a, severity=severity, **kw)

    app.notify = _notify
    return seen


def test_actions_target_visible_ticket(seeded_home):
    from textual.widgets import Input as _Input

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            app._selected_key = "T-3"
            seen = _spy_notify(app)
            app.push_screen(DetailScreen(seeded_home, "T-1"))
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            modal = app.screen_stack[-1]
            assert isinstance(modal, _InboxModal)
            modal.query_one("#inbox-input", _Input).value = "for T-1"
            await pilot.press("enter")
            await pilot.pause()
            assert [c["args"]["text"] for c in inbox.pending(seeded_home, "T-1")] == ["for T-1"]
            assert inbox.pending(seeded_home, "T-3") == []
            assert any("T-1" in m for m, _ in seen)
            assert app._exception is None

    asyncio.run(_inner())


def test_sort_by_fails_survives_refresh(home):
    _seed_sort_board(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            for _ in range(6):  # Key, Phase, Title, PR, CI, Fails
                await pilot.press(">")
            await pilot.pause()
            assert _row_values(app)[0] == "T-3"
            assert "↓ Fails" in _bar(app)
            order = _row_values(app)
            key = _cursor_key(app)
            app._populate()
            await pilot.pause()
            assert _row_values(app) == order
            assert _cursor_key(app) == key
            assert app._exception is None

    asyncio.run(_inner())


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


def test_sort_direction_flip(home):
    _seed_sort_board(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            for _ in range(6):
                await pilot.press(">")
            await pilot.press("<")
            await pilot.pause()
            assert _row_values(app)[-1] == "T-3"
            assert "↑ Fails" in _bar(app)
            assert app._exception is None

    asyncio.run(_inner())


def test_header_click_sorts_and_reverses(home):
    _seed_sort_board(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            cols = list(table.columns.values())
            x = sum(c.get_render_width(table) for c in cols[:5]) + 2
            await pilot.click("#tickets", offset=(x, 0))
            await pilot.pause()
            assert _row_values(app)[0] == "T-3"
            assert "↓ Fails" in _bar(app)
            await pilot.click("#tickets", offset=(x, 0))
            await pilot.pause()
            assert _row_values(app)[-1] == "T-3"
            assert "↑ Fails" in _bar(app)
            assert app._exception is None

    asyncio.run(_inner())


def test_empty_filter_clears_selection(home):
    seed_ticket(home, "T-3", "implementing", phase="implementing", pr=15)
    seed_ticket(home, "T-5", "ready", phase="ready")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            app._selected_key = "T-3"
            seen = _spy_notify(app)
            await pilot.press("f")
            await pilot.pause()
            assert _FILTERS[app._filter_idx][0] == "needs-you"
            assert app._selected_key is None
            assert "No tickets match" in app.query_one("#detail", Static).render().plain
            before = len(seen)
            await pilot.press("c")
            await pilot.pause()
            assert any(sev == "warning" for _, sev in seen[before:])
            await pilot.press("a")  # T-164: no target -> the decision queue, not a warning
            await pilot.pause()
            assert isinstance(app.screen, DecisionsScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert inbox.pending(home, "T-3") == [] and inbox.pending(home, "T-5") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_sort_cycle_returns_to_default_order(home):
    _seed_sort_board(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            for _ in range(8):  # through Idle
                await pilot.press(">")
            await pilot.pause()
            assert "Idle" in _bar(app)
            await pilot.press(">")
            await pilot.pause()
            assert _row_values(app) == [r[-1] for r in ticket_rows(home)]
            assert "↓" not in _bar(app) and "↑" not in _bar(app)
            assert app._exception is None

    asyncio.run(_inner())


def test_sort_keeps_cursor_on_selected_key(home):
    _seed_sort_board(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.move_cursor(row=table.get_row_index("T-5"))
            await pilot.pause()
            assert app._selected_key == "T-5"
            for _ in range(6):
                await pilot.press(">")
            await pilot.pause()
            assert app._selected_key == "T-5"
            assert _cursor_key(app) == "T-5"
            assert table.cursor_row == table.get_row_index("T-5")
            assert app._exception is None

    asyncio.run(_inner())


def test_ticket_actions_hidden_off_ticket_screens(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("F")
            await pilot.pause()
            assert isinstance(app.screen, FleetScreen)
            actions = {b.binding.action for b in app.screen.active_bindings.values()}
            assert not actions & {"answer", "discard"}
            await pilot.press("escape")
            await pilot.pause()
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            actions = {b.binding.action for b in app.screen.active_bindings.values()}
            assert "cycle_filter" not in actions
            assert app._exception is None

    asyncio.run(_inner())


def test_retry_dimmed_unless_degraded(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            assert app.check_action("retry", ()) is None
            assert app.check_action("discard", ()) is None
            app._selected_key = "T-2"
            assert app.check_action("retry", ()) is True
            app._selected_key = "T-1"
            assert app.check_action("answer", ()) is True
            app._selected_key = "T-3"
            assert app.check_action("answer", ()) is True  # T-164: opens the queue

    asyncio.run(_inner())


def test_question_mark_opens_help(seeded_home):
    from textual.widgets import HelpPanel

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert app.screen.query(HelpPanel)
            await pilot.press("question_mark")
            await pilot.pause()
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert app.screen.query(HelpPanel)
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# T-175: per-ticket hold via `h`                                              #
# --------------------------------------------------------------------------- #

def test_hold_key_roundtrip(seeded_home):
    from maestro import fleet

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], HoldModal)
            await pilot.press("3", "0", "m", "enter")
            await pilot.pause()
            await pilot.press(*"pairing", "enter")
            await pilot.pause()
            st = fleet.hold_state(seeded_home, "T-3", store.now_epoch())
            assert st is not None and st["reason"] == "pairing" and st["until"]
            app._filter_idx = _filter_idx("held")
            app._populate()
            await pilot.pause()
            table = app.query_one(DataTable)
            assert [str(rk.value) for rk in table.rows] == ["T-3"]
            assert "⏸" in str(table.get_row("T-3")[0])
            app._selected_key = "T-3"
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _ConfirmModal)
            await pilot.press("enter")
            await pilot.pause()
            assert fleet.hold_state(seeded_home, "T-3", store.now_epoch()) is not None
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            assert fleet.hold_state(seeded_home, "T-3", store.now_epoch()) is None
            assert app._exception is None

# T-163: live Now column, running filter, j jumps to running tickets          #
# --------------------------------------------------------------------------- #

import os as _os  # noqa: E402
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402


async def _live_tick(app, pilot):
    """Run one real worker refresh to completion and let its re-render land."""
    app._kick_live()
    await app.workers.wait_for_complete()
    await pilot.pause()


def _now_cell(app, key):
    table = app.query_one("#tickets", DataTable)
    return str(table.get_row(key)[-1])


def _claim(home, key, tmp_path, *, pid=None, kind=None, with_log=True):
    log = tmp_path / f"{key}.log"
    log.write_text("x")
    claims.write_claim(home, key, pid if pid is not None else _os.getpid(), "t",
                       log_path=str(log) if with_log else None, kind=kind)
    return log


def test_now_column_shows_live_claim(seeded_home, tmp_path):
    _claim(seeded_home, "T-3", tmp_path)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            await _live_tick(app, pilot)
            assert _now_cell(app, "T-3").startswith("●")
            assert "running(1)" in str(app.query_one("#filter-bar", Static).content)
            assert claims.read_claim(seeded_home, "T-3")
            assert app._exception is None

    asyncio.run(_inner())


def test_j_jumps_to_running_ticket(seeded_home, tmp_path):
    _claim(seeded_home, "T-3", tmp_path)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await _live_tick(app, pilot)
            assert app._filter_idx == _filter_idx("needs-you")
            await pilot.press("j")
            await pilot.pause()
            assert app._selected_key == "T-3"
            assert app._filter_idx == _filter_idx("all")
            table = app.query_one("#tickets", DataTable)
            assert str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value) == "T-3"
            idx = app._filter_idx
            await pilot.press("j")
            await pilot.pause()
            assert app._filter_idx == idx and app._selected_key == "T-3"
            assert app._exception is None

    asyncio.run(_inner())


def test_now_column_silence_thresholds_and_one_toast(seeded_home, tmp_path):
    log = _claim(seeded_home, "T-3", tmp_path)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            now = _time.time()
            _os.utime(log, (now - 150, now - 150))
            await _live_tick(app, pilot)
            cell = app.query_one("#tickets", DataTable).get_row("T-3")[-1]
            assert "silent" in str(cell) and "yellow" in str(cell.style)
            timeout = config_mod.load(str(seeded_home)).no_output_timeout
            _os.utime(log, (now - timeout * 0.6, now - timeout * 0.6))
            before = len(app._notifications)
            await _live_tick(app, pilot)
            await _live_tick(app, pilot)
            cell = app.query_one("#tickets", DataTable).get_row("T-3")[-1]
            assert "silent" in str(cell) and "red" in str(cell.style)
            toasts = [n for n in list(app._notifications)[before:] if "T-3" in n.message]
            assert len(toasts) == 1
            assert app._exception is None

    asyncio.run(_inner())


def test_now_column_dispatcher_owned_claims(seeded_home, tmp_path):
    _claim(seeded_home, "T-3", tmp_path, kind="testrun", with_log=False)
    _claim(seeded_home, "T-4", tmp_path, kind="restack", with_log=False)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            await _live_tick(app, pilot)
            assert _now_cell(app, "T-3") == "◌ tests"
            assert _now_cell(app, "T-4") == "◌ restack"
            assert "running(2)" in str(app.query_one("#filter-bar", Static).content)

    asyncio.run(_inner())


def test_live_worker_never_releases_claims(seeded_home, tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    _claim(seeded_home, "T-3", tmp_path, pid=proc.pid)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            app._filter_idx = _filter_idx("all")
            for _ in range(3):
                await _live_tick(app, pilot)
            assert "running(0)" in str(app.query_one("#filter-bar", Static).content)
            assert _now_cell(app, "T-3") == ""
            assert claims.read_claim(seeded_home, "T-3")
            assert app._exception is None

    asyncio.run(_inner())


def test_live_probe_runs_in_worker(seeded_home, tmp_path, monkeypatch):
    _claim(seeded_home, "T-3", tmp_path)
    main = _threading.get_ident()
    calls: list[tuple[str, int, bool]] = []
    in_populate = {"on": False}

    def _wrap(mod, name):
        real = getattr(mod, name)

        def _w(*a, **kw):
            calls.append((name, _threading.get_ident(), in_populate["on"]))
            return real(*a, **kw)
        monkeypatch.setattr(mod, name, _w)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            _wrap(claims, "probe_processes")
            _wrap(snap_mod, "load")
            real_populate = app._populate

            def _populate():
                in_populate["on"] = True
                try:
                    real_populate()
                finally:
                    in_populate["on"] = False
            app._populate = _populate
            for _ in range(3):
                await _live_tick(app, pilot)
            names = {c[0] for c in calls}
            assert names == {"probe_processes", "load"}
            assert not any(c[2] for c in calls), "disk/ps read while _populate ran"
            assert all(c[1] != main for c in calls), "refresh-driven read on the UI thread"
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# T-173: ActivityScreen (board-wide ticker)                                   #
# --------------------------------------------------------------------------- #

async def _open_activity(app, pilot):
    ActivityScreen.POLL_INTERVAL = 0.2
    await pilot.press("T")
    for _ in range(40):
        await pilot.pause(0.05)
        if isinstance(app.screen, ActivityScreen) and app.screen._ready:
            break
    await pilot.pause(0.2)
    assert isinstance(app.screen, ActivityScreen)
    return app.screen


async def _wait_for(pilot, cond, tries=60):
    for _ in range(tries):
        await pilot.pause(0.1)
        if cond():
            return True
    return False


@pytest.fixture(autouse=False)
def _fast_activity(monkeypatch):
    monkeypatch.setattr(ActivityScreen, "POLL_INTERVAL", 0.2)


def _row_keys(screen):
    return [k.value for k in screen.query_one("#act-table", DataTable).rows]


def test_activity_screen_tails_new_event_to_top(seeded_home, _fast_activity):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            screen = await _open_activity(app, pilot)
            assert _row_keys(screen)
            ev = event_log.append(seeded_home, "T-3", "Note", {"text": "hello [x]"}, actor="t")
            top = f"T-3:{ev['seq']}"
            assert await _wait_for(pilot, lambda: _row_keys(screen)[:1] == [top])
            assert _row_keys(screen).count(top) == 1
            assert app._exception is None

    asyncio.run(_inner())


def test_activity_hot_keys_pause_and_categories(seeded_home, _fast_activity):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            screen = await _open_activity(app, pilot)
            for i in range(40):
                event_log.append(seeded_home, "T-3", "RequeueScheduled", {"at": i}, actor="t")
            assert await _wait_for(pilot, lambda: screen.hot_keys[:1] and screen.hot_keys[0][0] == "T-3"
                                   and screen.hot_keys[0][1] >= 40)
            await pilot.press("space")
            before = len(_row_keys(screen))
            ev = event_log.append(seeded_home, "T-4", "Note", {"text": "held"}, actor="t")
            held = f"T-4:{ev['seq']}"
            assert await _wait_for(pilot, lambda: screen._held)
            assert len(_row_keys(screen)) == before and held not in _row_keys(screen)
            await pilot.press("space")
            await pilot.pause()
            assert _row_keys(screen)[0] == held
            await pilot.press("6")  # RequeueScheduled lives in group 6
            await pilot.pause()
            assert not any(k for k in _row_keys(screen)
                           if screen.query_one("#act-table", DataTable).get_row(k)[2].plain == "RequeueScheduled")
            assert app._exception is None

    asyncio.run(_inner())


def _tree_sizes(home):
    return {str(p): p.stat().st_size for d in ("events", "derived/claims")
            if (home / d).is_dir() for p in (home / d).rglob("*") if p.is_file()}


def test_activity_enter_opens_detail_read_only(seeded_home, _fast_activity):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            screen = await _open_activity(app, pilot)
            table = screen.query_one("#act-table", DataTable)
            idx = next(i for i, k in enumerate(_row_keys(screen)) if k.startswith("T-3:"))
            table.move_cursor(row=idx)
            await pilot.pause()
            before = _tree_sizes(seeded_home)
            depth = len(app.screen_stack)
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == depth + 1
            assert isinstance(app.screen, DetailScreen) and app.screen._key == "T-3"
            assert app._selected_key == "T-1"
            await pilot.pause(0.5)
            assert _tree_sizes(seeded_home) == before
            assert app._exception is None

    asyncio.run(_inner())




# --------------------------------------------------------------------------- #
# T-172: live DetailScreen, event table, payload modal                        #
# --------------------------------------------------------------------------- #

def _ds_cursor_seq(table):
    return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value


def test_detail_screen_live_refresh_keeps_cursor(seeded_home, monkeypatch):
    monkeypatch.setattr(DetailScreen, "REFRESH_INTERVAL", 0.2)
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#ds-events", DataTable)
            before_rows = table.row_count
            table.move_cursor(row=1)
            await pilot.pause()
            seq = _ds_cursor_seq(table)
            event_log.append(seeded_home, "T-3", "PhaseChanged",
                             {"phase": "qa", "reason": "x"}, actor="r")
            await pilot.pause(0.8)
            assert table.row_count == before_rows + 1
            assert _ds_cursor_seq(table) == seq
            assert "qa" in str(screen.query_one("#ds-dwell", Static).render())
            assert app._exception is None
    asyncio.run(_inner())


def test_detail_screen_refresh_gated_on_log_signature(seeded_home, monkeypatch):
    monkeypatch.setattr(DetailScreen, "REFRESH_INTERVAL", 0.2)
    async def _inner():
        for _ in range(30):
            event_log.append(seeded_home, "T-3", "Checked", {"n": 1}, actor="r")
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 20)) as pilot:
            await pilot.pause()
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            screen = app.screen
            screen._tail_mode = False
            screen.action_refresh()
            table = screen.query_one("#ds-events", DataTable)
            await pilot.pause()
            table.move_cursor(row=table.row_count - 1)
            await pilot.pause()
            table.move_cursor(row=3)
            await pilot.pause()
            calls = []
            orig = screen._refresh
            screen._refresh = lambda: (calls.append(1), orig())
            state = (table.cursor_row, table.scroll_y)
            await pilot.pause(0.7)
            assert not calls, "refresh must not re-render without a log change"
            assert (table.cursor_row, table.scroll_y) == state
            assert app._exception is None
    asyncio.run(_inner())


def test_detail_event_row_opens_payload_modal(seeded_home):
    async def _inner():
        excerpt = "boom [red]x[/red] " + "y" * 600
        event_log.append(seeded_home, "T-3", "CiObserved",
                         {"state": "failure", "failure_excerpt": excerpt}, actor="d")
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            table = app.screen.query_one("#ds-events", DataTable)
            table.focus()
            table.move_cursor(row=table.row_count - 1)
            await pilot.pause()
            depth = len(app.screen_stack)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, _EventPayloadModal)
            assert len(app.screen_stack) == depth + 1
            assert excerpt in str(app.screen.query_one("#payload-json", Static).render())
            await pilot.press("c")
            await pilot.pause()
            assert json.loads(app.clipboard)["payload"]["failure_excerpt"] == excerpt
            assert app._selected_key == "T-3"
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen)
            assert sum(isinstance(sc, DetailScreen) for sc in app.screen_stack) == 1
            assert app._exception is None
    asyncio.run(_inner())


def test_events_screen_table_and_payload_modal(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.push_screen(DetailScreen(seeded_home, "T-3"))
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, EventsScreen)
            assert app.screen._key == "T-3"
            table = app.screen.query_one("#events-full", DataTable)
            assert table.row_count == len(event_log.read(seeded_home, "T-3"))
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, _EventPayloadModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, EventsScreen)
            assert app._exception is None
    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# T-167: AC evidence matrix (AcScreen, `v`)                                    #
# --------------------------------------------------------------------------- #

def _seed_ac_ticket(home, key="T-9", *, worktree=False):
    from maestro.config import Config
    store.atomic_write(
        store.spec_path(home, key),
        f"# {key}: ac matrix\npriority: 2\n\n## Intent\nx\n\n## Acceptance criteria\n"
        "- [ ] first shows [bold] literally\n- [ ] second fails qa\n"
        "- [ ] third (test: tests/test_x.py::test_x)\n")
    event_log.append(home, key, "TicketCreated", {"title": "ac matrix", "source": "test"}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": "qa", "reason": ""}, actor="r")
    snap_mod.rebuild(home, key)
    cfg = Config(home=home)
    acs = snap_mod.parse_acs(store.spec_path(home, key).read_text())
    ops_mod.verify_ac(cfg, key, 1, {"what": "ran", "where": "t.py", "result": "ok"})
    ops_mod.record_qa_verdict(cfg, key, 2, "fail", "second is broken")
    event_log.append(home, key, "AcCheckCaptured", {
        "tree_key": "1234567abc:dead", "ac_hash": snap_mod.ac_hash(acs[2]), "ac_index": 3,
        "ac_text": acs[2], "kind": "test", "command": "pytest tests/test_x.py", "exit_code": 1,
        "passed": False, "failure_excerpt": "AssertionError: kaboom"}, actor="d")
    snap_mod.rebuild(home, key)
    if worktree:
        store.worktree_path(home, key).mkdir(parents=True)


def _table_text(app):
    t = app.screen.query_one("#ac-table", DataTable)
    return [[str(c) for c in t.get_row_at(i)] for i in range(t.row_count)]


def test_ac_matrix_rows_and_header(home):
    _seed_ac_ticket(home)
    seed_ticket(home, "T-1", "other")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, AcScreen)
            assert str(app.screen.query_one("#ac-summary", Static).render()) == \
                "QA spec 0/3 pass · 1 fail · 2 pending"
            rows = _table_text(app)
            assert [r[0] for r in rows] == ["1", "2", "3"]
            assert rows[0][1] == "first shows [bold] literally"
            assert "fail" in rows[1][3]
            assert app._exception is None

    asyncio.run(_inner())


def test_ac_matrix_targets_visible_ticket(home):
    _seed_ac_ticket(home)
    seed_ticket(home, "T-1", "other")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            app.push_screen(SpecScreen(home, "T-9"))
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, AcScreen)
            assert app.screen._key == "T-9"
            assert app._exception is None

    asyncio.run(_inner())


def test_ac_matrix_evidence_modal(home):
    _seed_ac_ticket(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            assert isinstance(app.screen, _AcEvidenceModal)
            body = " ".join(str(w.render()) for w in app.screen.query(Static))
            assert "AssertionError: kaboom" in body
            assert "pytest tests/test_x.py" in body
            assert app._exception is None

    asyncio.run(_inner())


def test_ac_matrix_message_writes_no_evidence(home):
    _seed_ac_ticket(home)
    before = len(event_log.read(home, "T-9"))

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("down", "m")
            await pilot.pause()
            assert isinstance(app.screen, _InboxModal)
            inp = app.screen.query_one("#inbox-input", Input)
            assert inp.value == "re AC2: "
            inp.value = "re AC2: please look"
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app._exception is None

    asyncio.run(_inner())
    msgs = [e for e in inbox.pending(home, "T-9") if e["command"] == "msg"]
    assert len(msgs) == 1 and msgs[0]["args"]["text"] == "re AC2: please look"
    assert len(event_log.read(home, "T-9")) == before


def test_ac_matrix_add_ac_and_editor_line(home, monkeypatch):
    from maestro.tui.screens import editor_argv
    monkeypatch.setenv("VISUAL", "vim")
    assert editor_argv(Path("/p/spec.md"), line=7) == ["vim", "+7", "/p/spec.md"]
    assert editor_argv(Path("/p/spec.md")) == ["vim", "/p/spec.md"]
    _seed_ac_ticket(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            assert len(_table_text(app)) == 3
            await pilot.press("A")
            await pilot.pause()
            assert isinstance(app.screen, _AddAcModal)
            app.screen.query_one("#add-ac-input", Input).value = "fourth one"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, AcScreen)
            assert len(_table_text(app)) == 4
            assert app._exception is None

    asyncio.run(_inner())


def test_ac_matrix_edit_passes_ac_line_to_editor(home, monkeypatch):
    import maestro.tui.screens as screens_mod
    _seed_ac_ticket(home)
    seen = {}
    monkeypatch.setattr(screens_mod, "edit_in_editor",
                        lambda app, path, line=None: seen.update(line=line))

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("down", "E")
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    lines = store.spec_path(home, "T-9").read_text().splitlines()
    assert lines[seen["line"] - 1].startswith("- [ ] second")


def test_ac_matrix_no_worktree_is_read_only(home):
    _seed_ac_ticket(home)
    before = len(event_log.read(home, "T-9"))

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-9"
            await pilot.press("v")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert "no worktree" in _table_text(app)[2][5]
            assert app._exception is None

    asyncio.run(_inner())
    assert not (home / "scratch").exists()
    assert len(event_log.read(home, "T-9")) == before



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


# --------------------------------------------------------------------------- #
# T-179: spec priority/dependsOn modal ('M')                                  #
# --------------------------------------------------------------------------- #

def _open_spec_fields(app):
    from maestro.tui.modals import _SpecFieldsModal
    modal = app.screen_stack[-1]
    assert isinstance(modal, _SpecFieldsModal)
    return modal


def test_spec_fields_modal_sets_depends_on(seeded_home):
    from textual.widgets import SelectionList

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            app._selected_key = "T-5"
            await pilot.press("M")
            await pilot.pause()
            modal = _open_spec_fields(app)
            modal.query_one("#spec-deps", SelectionList).select("T-3")
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1
            table = app.query_one("#tickets", DataTable)
            col = [c.label.plain for c in table.columns.values()].index("Deps")
            assert str(table.get_row("T-5")[col]) == "1"

    asyncio.run(_inner())
    assert "dependsOn: [T-3]" in store.spec_path(seeded_home, "T-5").read_text()


def test_spec_fields_modal_blocks_cycle(seeded_home):
    from textual.widgets import Label, SelectionList
    assert cli_main(["--home", str(seeded_home), "spec-set", "T-3", "--depends-on", "T-5"]) == 0
    before = store.spec_path(seeded_home, "T-5").read_bytes()

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await pilot.press("M")
            await pilot.pause()
            modal = _open_spec_fields(app)
            modal.query_one("#spec-deps", SelectionList).select("T-3")
            await pilot.pause()
            assert "CYCLE:" in str(modal.query_one("#spec-preview", Label).render())
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app._exception is None
            assert app.screen_stack[-1] is modal  # Save refused, still open

    asyncio.run(_inner())
    assert store.spec_path(seeded_home, "T-5").read_bytes() == before


def test_spec_fields_modal_keeps_done_deps(seeded_home):
    from textual.widgets import Label
    seed_ticket(seeded_home, "T-9", "finished", phase="done")
    path = store.spec_path(seeded_home, "T-5")
    assert cli_main(["--home", str(seeded_home), "spec-set", "T-5", "--depends-on", "T-9"]) == 0

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-5"
            await pilot.press("M")
            await pilot.pause()
            modal = _open_spec_fields(app)
            assert "kept: T-9" in str(modal.query_one("#spec-kept", Label).render())
            modal.query_one("#spec-priority", Input).value = "1"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app._exception is None
            assert len(app.screen_stack) == 1

    asyncio.run(_inner())
    text = path.read_text()
    assert "dependsOn: [T-9]" in text and "priority: 1" in text


# --------------------------------------------------------------------------- #
# T-165: Why/Next screen (WhyScreen, `w`)                                      #
# --------------------------------------------------------------------------- #

def _why(app, sel):
    return str(app.screen.query_one(sel, Static).render())


def _seed_with_dep(home, key, dep, dep_phase="implementing"):
    for k, phase, deps in ((dep, dep_phase, ""), (key, "ready", dep)):
        store.atomic_write(store.spec_path(home, k),
                           f"# {k}\npriority: 2\ndependsOn: [{deps}]\n\n"
                           "## Acceptance criteria\n- [ ] ok\n")
        event_log.append(home, k, "TicketCreated",
                         {"title": k, "spec_hash": disp_mod.spec_hash_on_disk(home, k)}, actor="d")
        event_log.append(home, k, "PhaseChanged", {"phase": phase}, actor="r")
        snap_mod.rebuild(home, k)


def test_why_screen_blocked_dep(home):
    _seed_with_dep(home, "T-1", "T-2")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await pilot.press("w")
            await _settle(app, pilot)
            assert isinstance(app.screen, WhyScreen)
            now = _why(app, "#why-now")
            assert "blocked-dep" in now and "T-2 (implementing)" in now
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, WhyScreen) and app.screen._key == "T-2"
            assert len(app.screen_stack) == 3
            assert app._exception is None

    asyncio.run(_inner())


def test_why_screen_backoff_flips_to_timer(home):
    seed_phase(home, "T-1", Phase.AWAITING_CI)
    t0 = store.now_epoch()
    ops_mod.requeue(config_mod.load(str(home)), "T-1", 600)
    clock = [t0]

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.push_screen(WhyScreen(home, "T-1", clock=lambda: clock[0]))
            await _settle(app, pilot)
            assert "NOT DUE: backoff" in _why(app, "#why-now")
            clock[0] = snap_mod.load(home, "T-1").next_requeue_at + 1
            await pilot.pause(1.3)
            assert "DUE: timer" in _why(app, "#why-now")
            assert "NOT DUE" not in _why(app, "#why-now")
            assert app._exception is None

    asyncio.run(_inner())


def test_why_screen_throttle_line(home):
    (home / "config.toml").write_text("[maestro]\nmin_spawn_interval = 300\n", encoding="utf-8")
    seed_phase(home, "T-1", Phase.READY)
    now = store.now_epoch()
    store.write_json(disp_mod._spawn_ledger_path(home),
                     {"T-1": {"last": now - 60, "recent": [[now - 60, 1]], "phase": "ready"}})

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await pilot.press("w")
            await _settle(app, pilot)
            line = _why(app, "#why-throttle")
            assert "next spawn allowed in" in line and "PAUSED" not in line
            fleet_mod.pause(home, reason="maintenance")
            await pilot.press("r")
            await _settle(app, pilot)
            line = _why(app, "#why-throttle")
            assert "next spawn allowed in" in line and "fleet PAUSED (maintenance)" in line
            assert app._exception is None

    asyncio.run(_inner())


def _seed_decisions(home, key, n):
    lines = [json.dumps({"ts": f"2026-10-08T00:{i:02d}:00+00:00",
                         "decisions": {key: {"outcome": "not_due", "reason": f"reason-{i}"},
                                       "T-other": {"outcome": "spawned", "reason": "x"}}})
             for i in range(n)]
    (home / "derived").mkdir(exist_ok=True)
    disp_mod.dispatch_ledger_path(home).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_why_screen_decisions_table(home):
    seed_phase(home, "T-1", Phase.READY)
    _seed_decisions(home, "T-1", 25)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await pilot.press("w")
            await _settle(app, pilot)
            table = app.screen.query_one("#why-decisions", DataTable)
            assert table.row_count == 20
            reasons = [str(table.get_row_at(i)[2]) for i in range(20)]
            assert "reason-24" in reasons and "reason-5" in reasons and "reason-4" not in reasons
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, _AcEvidenceModal)
            body = " ".join(str(w.render()) for w in app.screen.query(Static))
            assert "reason-24" in body
            assert app._exception is None

    asyncio.run(_inner())


def test_why_screen_is_read_only(home):
    seed_phase(home, "T-1", Phase.READY)
    seed_phase(home, "T-2", Phase.READY)
    _seed_decisions(home, "T-1", 3)
    disp_mod._write_heartbeat(home, store.now_epoch(), 0, 0)
    claims.write_claim(home, "T-2", 999999, "dead")  # stale: pid is not alive

    def _state():
        claim_files = sorted((p.name, p.stat().st_mtime_ns)
                             for p in (home / "derived" / "claims").glob("*.json"))
        return ((home / "derived" / ".heartbeat.json").stat().st_mtime_ns,
                len(disp_mod.dispatch_ledger_path(home).read_text().splitlines()), claim_files)

    before = _state()

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-2"
            await pilot.press("w")
            await _settle(app, pilot)
            assert "STALE" in _why(app, "#why-claim")
            await pilot.press("r")
            await _settle(app, pilot)
            await pilot.press("r")
            await _settle(app, pilot)
            assert app._exception is None

    asyncio.run(_inner())
    after = _state()
    assert claims.claim_path(home, "T-2").exists()
    assert after == before


def test_why_screen_kick_is_confirmed_and_scoped(home):
    seed_phase(home, "T-1", Phase.READY)
    seed_phase(home, "T-2", Phase.READY)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await pilot.press("w")
            await _settle(app, pilot)
            await pilot.press("k")
            await pilot.pause()
            assert isinstance(app.screen, _ConfirmModal)
            await pilot.press("enter")  # default No
            await _settle(app, pilot)
            assert isinstance(app.screen, WhyScreen)
            assert app.dry.spawned == []
            await pilot.press("k")
            await pilot.pause()
            await pilot.press("y")
            await _settle(app, pilot)
            assert [sp[0] for sp in app.dry.spawned] == ["T-1"]
            assert app._exception is None

    asyncio.run(_inner())






# --------------------------------------------------------------------------- #
# T-166: `/` live filter                                                       #
# --------------------------------------------------------------------------- #

def _table_keys(app):
    table = app.query_one("#tickets", DataTable)
    return [str(table.get_row_at(r)[0]) for r in range(table.row_count)]


async def _type_query(pilot, text):
    await pilot.press("slash")
    await pilot.press(*text)
    await pilot.pause(0.4)


def test_slash_filter_phase_query(home):
    seed_ticket(home, "S-1", "one", phase="ready")
    seed_ticket(home, "S-2", "two", phase="ready")
    seed_ticket(home, "S-3", "three", phase="qa")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._populate()
            assert len(_table_keys(app)) == 3
            await _type_query(pilot, "phase:ready")
            assert sorted(_table_keys(app)) == ["S-1", "S-2"]
            bar = str(app.query_one("#filter-bar", Static).render())
            assert "/ phase:ready" in bar and "(2)" in bar
            assert app._exception is None

    asyncio.run(_inner())


def test_slash_filter_input_swallows_app_keys(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            idx = app._filter_idx
            await _type_query(pilot, "q")
            await pilot.press("f")
            await pilot.pause(0.4)
            assert app.is_running and app._filter_idx == idx
            assert app.query_one("#query-bar", Input).value == "qf"
            assert app._exception is None

    asyncio.run(_inner())


def test_slash_filter_enter_keeps_esc_clears(home):
    seed_ticket(home, "S-1", "one", phase="ready")
    seed_ticket(home, "S-3", "three", phase="qa")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._populate()
            await _type_query(pilot, "phase:qa")
            await pilot.press("enter")
            await pilot.pause()
            assert app.focused is app.query_one("#tickets", DataTable)
            await pilot.press("r")
            await pilot.pause(0.4)
            assert _table_keys(app) == ["S-3"]
            await pilot.press("escape")
            await pilot.pause()
            assert sorted(_table_keys(app)) == ["S-1", "S-3"]
            assert not app.query_one("#query-bar", Input).display
            assert "/ phase" not in str(app.query_one("#filter-bar", Static).render())
            assert app._exception is None

    asyncio.run(_inner())


def test_slash_filter_combines_with_preset(home):
    seed_ticket(home, "S-1", "asks", phase="awaiting-human", questions={"q1": "ok?"})
    seed_ticket(home, "S-2", "stuck", phase="degraded")
    seed_ticket(home, "S-3", "busy", phase="implementing")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert _FILTERS[app._filter_idx][0] == "needs-you"
            assert sorted(_table_keys(app)) == ["S-1", "S-2"]
            await _type_query(pilot, "!q:open")
            assert _table_keys(app) == ["S-2"]
            assert app._exception is None

    asyncio.run(_inner())


def test_slash_filter_empty_and_invalid(home):
    seed_ticket(home, "S-1", "one", phase="ready")
    seed_ticket(home, "S-2", "two", phase="qa")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._populate()
            await _type_query(pilot, "phase:qa")
            assert _table_keys(app) == ["S-2"]
            await pilot.press("x")  # "phase:qax" matches nothing
            await pilot.pause(0.4)
            assert _table_keys(app) == [] and app._selected_key is None
            await pilot.press("backspace", "backspace", "backspace", "backspace", "backspace",
                              "backspace", "backspace", "backspace", "backspace", "backspace")
            await pilot.press(*"bogus:1")
            await pilot.pause(0.4)
            assert app._query_error
            assert "bogus" in str(app.query_one("#filter-bar", Static).render())
            assert app._exception is None

    asyncio.run(_inner())


def test_slash_filter_board_only(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert not app._on_board()
            await pilot.press("slash")
            await pilot.pause()
            assert not app.query_one("#query-bar", Input).display
            assert app._exception is None

    asyncio.run(_inner())

# --------------------------------------------------------------------------- #
# T-164: board-wide decision queue (W)                                        #
# --------------------------------------------------------------------------- #

def _seed_dq(home):
    seed_phase(home, "T-1", Phase.TRIAGING)
    rc = cli_main(["--home", str(home), "ask", "T-1",
                   "--question", "Proceed with A?", "Yes, A", "qa1", "proceed",
                   "--question", "Which colour?", "blue", "qa2", "other"])
    assert rc == 0
    seed_phase(home, "T-6", Phase.TRIAGING)
    assert cli_main(["--home", str(home), "ask", "T-6", "--question", "Ship it?", "Yes", "q6",
                     "proceed"]) == 0
    seed_phase(home, "T-2", Phase.DEGRADED)


async def _open_decisions(app, pilot):
    await pilot.pause()
    await pilot.press("W")
    await pilot.pause()
    assert isinstance(app.screen, DecisionsScreen)
    await app.workers.wait_for_complete()
    await pilot.pause()
    return app.screen


def _dq_rows(screen):
    table = screen.query_one("#dq-table", DataTable)
    return [tuple(str(c) for c in table.get_row_at(i)) for i in range(table.row_count)]


def _ans(home, key):
    return [c for c in inbox.pending(home, key) if c["command"] == "ans"]


def test_decisions_screen_lists_open_questions(home):
    _seed_dq(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await _open_decisions(app, pilot)
            table = screen.query_one("#dq-table", DataTable)
            assert [str(c.label) for c in table.columns.values()] == [
                "Key", "Pos", "Kind", "Age", "Question", "Rec"]
            rows = _dq_rows(screen)
            assert [r[0] for r in rows] == ["T-1", "T-1", "T-2", "T-6"]
            assert [r[1] for r in rows[:2]] == ["1/2", "2/2"]
            assert [r[2] for r in rows] == ["proceed", "other", "degraded", "proceed"]
            assert rows[0][5] == "Yes, A" and rows[0][3] != "…"
            assert app._exception is None

    asyncio.run(_inner())


def test_decisions_accept_recommendation_once(home):
    _seed_dq(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await _open_decisions(app, pilot)
            table = screen.query_one("#dq-table", DataTable)
            await pilot.press("y")
            await pilot.pause()
            snap = snap_mod.load(home, "T-1")
            ans = _ans(home, "T-1")
            assert len(ans) == 1
            qid = ans[0]["args"]["qid"]
            assert ans[0]["args"]["text"] == snap.question_recommends[qid]
            assert ans[0]["args"]["accepted_recommendation"] is True
            assert table.cursor_row == 1
            await pilot.press("up", "y")
            await pilot.pause()
            assert len(_ans(home, "T-1")) == 1
            assert _dq_rows(screen)[0][5] == "queued"
            assert app._exception is None

    asyncio.run(_inner())


def test_decisions_bulk_accept_proceed_only(home):
    _seed_dq(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await _open_decisions(app, pilot)
            await pilot.press("space", "down", "space", "Y")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, _ConfirmModal)
            assert "partial: will spawn" in modal._message and "T-1" in modal._message
            await pilot.press("enter")  # default No
            await pilot.pause()
            assert screen is app.screen and _ans(home, "T-1") == []
            await pilot.press("Y")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            ans = _ans(home, "T-1")
            assert [a["args"]["qid"] for a in ans] == ["qa1"]
            assert app._exception is None

    asyncio.run(_inner())


def test_decisions_retry_discard_degraded(home):
    _seed_dq(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await _open_decisions(app, pilot)
            await pilot.press("down", "down")  # T-2
            assert screen.current_key() == "T-2"
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, _ConfirmModal)
            await pilot.press("enter")
            await pilot.pause()
            assert inbox.pending(home, "T-2") == []
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press(*"T-2")
            await pilot.press("enter")
            await pilot.pause()
            assert [c["command"] for c in inbox.pending(home, "T-2")] == ["discard"]
            assert app._exception is None
        app = _make_app(home)
        home2 = home
        # retry on a fresh queue (the discard above is still pending -> use T-2 again after ack)
        inbox.ack(home2, "T-2")
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await _open_decisions(app, pilot)
            await pilot.press("down", "down", "r")
            await pilot.pause()
            assert [c["command"] for c in inbox.pending(home, "T-2")] == ["retry"]
            await pilot.press("up", "r")  # non-degraded row: warn, write nothing
            await pilot.pause()
            assert inbox.pending(home, "T-1") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_decisions_close_nudges_once(home):
    _seed_dq(home)
    seed_phase(home, "T-5", Phase.READY)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            toasts = _capture_toasts(app)
            await _open_decisions(app, pilot)
            await pilot.press("escape")  # nothing written -> nothing nudged
            await _settle(app, pilot)
            assert app.dry.spawned == [] and not any("answered" in t for t in toasts)
            await pilot.press("W")
            await pilot.pause()
            await pilot.press("y", "down", "down", "down", "y")  # T-1/qa1 and T-6
            await pilot.pause()
            assert app.dry.spawned == []
            await pilot.press("escape")
            await _settle(app, pilot)
            assert sorted(set(s[0] for s in app.dry.spawned)) <= ["T-1", "T-6"]
            assert len([s for s in app.dry.spawned if s[0] == "T-1"]) <= 1
            assert not any(s[0] == "T-5" for s in app.dry.spawned)
            assert len([t for t in toasts if "answered" in t]) == 1
            assert app._exception is None

    asyncio.run(_inner())


def test_answer_key_opens_decision_queue(home):
    _seed_dq(home)
    seed_ticket(home, "T-3", "implementing", phase="implementing")

    async def _inner():
        from textual.widgets import Input as _Input
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._refresh_now()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            table.move_cursor(row=[str(k.value) for k in table.rows].index("T-3"))
            await pilot.pause()
            assert app._selected_key == "T-3"
            await pilot.press("a")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DecisionsScreen)
            assert _ans(home, "T-3") == []
            await pilot.press("down", "down")  # T-2
            await pilot.press("i")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, _InboxModal)
            modal.query_one("#inbox-input", _Input).value = "hello"
            await pilot.press("enter")
            await pilot.pause()
            assert [c["args"]["text"] for c in inbox.pending(home, "T-2")] == ["hello"]
            assert inbox.pending(home, "T-3") == []
            assert app._exception is None

    asyncio.run(_inner())


def test_decisions_footer_and_resize_bindings():
    from textual.binding import Binding
    visible = {_bkey(b) for b in MaestroTUI.BINDINGS if not isinstance(b, Binding) or b.show}
    assert "W" in visible and "[" not in visible and "]" not in visible
    assert len(visible) <= 10
