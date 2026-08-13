"""Tests for `maestro tui` entrypoint."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

from maestro import claims, event_log, inbox, snapshot as snap_mod, store
from maestro.cli import main
from maestro.config import Config
from maestro.projection import ticket_rows
from maestro.statemachine import Phase, ACTIVE_PHASES


def test_tui_missing_package_exits_2(tmp_path, capsys):
    """maestro tui exits 2 with an install hint when textual is not installed."""
    saved = sys.modules.pop("maestro.tui", None)
    sys.modules["maestro.tui"] = None  # None sentinel → ImportError on import

    try:
        rc = main(["--home", str(tmp_path), "tui"])
    finally:
        del sys.modules["maestro.tui"]
        if saved is not None:
            sys.modules["maestro.tui"] = saved

    assert rc == 2
    assert "pip install" in capsys.readouterr().err


def test_tui_install_hint_mentions_extra(tmp_path, capsys):
    """The install hint names the [tui] extra so users know what to install."""
    saved = sys.modules.pop("maestro.tui", None)
    sys.modules["maestro.tui"] = None

    try:
        main(["--home", str(tmp_path), "tui"])
    finally:
        del sys.modules["maestro.tui"]
        if saved is not None:
            sys.modules["maestro.tui"] = saved

    assert "[tui]" in capsys.readouterr().err


def test_tui_core_cli_unaffected_by_missing_textual(tmp_path, capsys):
    """Core CLI commands still work when textual is missing."""
    saved = sys.modules.pop("maestro.tui", None)
    sys.modules["maestro.tui"] = None

    try:
        # `maestro env` is a core command that must work regardless
        rc = main(["--home", str(tmp_path), "env"])
    finally:
        del sys.modules["maestro.tui"]
        if saved is not None:
            sys.modules["maestro.tui"] = saved

    assert rc == 0


def test_tui_delegates_to_tui_main(tmp_path):
    """When textual is present, cmd_tui calls tui.main with the parsed args."""
    fake_main = mock.Mock(return_value=0)
    fake_tui_module = mock.MagicMock()
    fake_tui_module.main = fake_main

    saved = sys.modules.pop("maestro.tui", None)
    sys.modules["maestro.tui"] = fake_tui_module

    try:
        rc = main(["--home", str(tmp_path), "tui"])
    finally:
        del sys.modules["maestro.tui"]
        if saved is not None:
            sys.modules["maestro.tui"] = saved

    assert rc == 0
    fake_main.assert_called_once()


def test_tui_passes_home_to_main(tmp_path):
    """The --home argument is forwarded to tui.main as part of args."""
    received = []

    def capture_main(args):
        received.append(args)
        return 0

    fake_tui_module = mock.MagicMock()
    fake_tui_module.main = capture_main

    saved = sys.modules.pop("maestro.tui", None)
    sys.modules["maestro.tui"] = fake_tui_module

    try:
        main(["--home", str(tmp_path), "tui"])
    finally:
        del sys.modules["maestro.tui"]
        if saved is not None:
            sys.modules["maestro.tui"] = saved

    assert received
    assert received[0].home == str(tmp_path)


# --- ticket_rows (data layer, no textual required) ---------------------------

def test_ticket_rows_empty_home(home):
    """An empty home returns an empty list without crashing."""
    assert ticket_rows(home) == []


def test_ticket_rows_lists_ticket_columns(home):
    """ticket_rows returns one row per ticket with key/phase/title in the correct positions."""
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "My feature", "source": "test", "spec_hash": "abc"},
                     actor="test", step_id="tc-1")
    snap_mod.rebuild(home, "T-1")

    rows = ticket_rows(home)
    assert len(rows) == 1
    key, phase, title, pr, ci, tier, fails, row_key = rows[0]
    assert key == "T-1"
    assert phase == Phase.TRIAGING.value
    assert title == "My feature"
    assert pr == "—"
    assert ci == "—"
    assert tier == "1"  # no spec.md on disk -> spec_tier's documented fallback
    assert row_key == "T-1"


def test_ticket_rows_tier_sourced_from_spec_not_snapshot(home):
    """The Tier column is rendered from `dispatcher.spec_tier` (the spec on disk),
    not any snapshot field -- 0/2/malformed all render per spec_tier's contract,
    with the falsy-0 bug fixed (0 must render as "0", not "—")."""
    store.atomic_write(store.spec_path(home, "Z-0"), "# Z-0\napproval_tier: 0\n")
    store.atomic_write(store.spec_path(home, "Z-2"), "# Z-2\napproval_tier: 2\n")
    store.atomic_write(store.spec_path(home, "Z-3"), "# Z-3\napproval_tier: nope\n")
    for key in ("Z-0", "Z-2", "Z-3"):
        event_log.append(home, key, "TicketCreated", {"title": key}, actor="d")
        snap_mod.rebuild(home, key)

    rows = {r[0]: r[5] for r in ticket_rows(home)}
    assert rows["Z-0"] == "0"       # falsy-0 bug: must not render "—"
    assert rows["Z-2"] == "2"
    assert rows["Z-3"] == "1"       # malformed -> spec_tier's safe fallback

    # Editing the spec on disk (no new event) changes the rendered tier on the
    # very next projection read -- the fold never saw a tier-carrying event.
    events_before = len(event_log.read(home, "Z-0"))
    store.atomic_write(store.spec_path(home, "Z-0"), "# Z-0\napproval_tier: 2\n")
    rows = {r[0]: r[5] for r in ticket_rows(home)}
    assert rows["Z-0"] == "2"
    assert len(event_log.read(home, "Z-0")) == events_before  # no new event appended


def test_ticket_rows_pr_label(home):
    """A ticket with a PR shows a clickable link to it, one without shows '—'."""
    event_log.append(home, "T-1", "PrOpened",
                     {"number": 42, "url": "https://github.com/x/y/pull/42", "draft": True},
                     actor="r", step_id="pr-T-1")
    snap_mod.rebuild(home, "T-1")

    rows = ticket_rows(home)
    assert rows[0][3] == "[link=https://github.com/x/y/pull/42]#42[/link]"


def test_ticket_rows_phase_order(home):
    """Rows are sorted by WORKSTATE phase order (implementing before triaging)."""
    event_log.append(home, "A-1", "TicketCreated", {"title": "alpha"}, actor="d")
    event_log.append(home, "A-1", "PhaseChanged", {"phase": "triaging", "reason": ""},
                     actor="r")
    snap_mod.rebuild(home, "A-1")

    event_log.append(home, "B-1", "TicketCreated", {"title": "beta"}, actor="d")
    event_log.append(home, "B-1", "PhaseChanged", {"phase": "implementing", "reason": ""},
                     actor="r")
    snap_mod.rebuild(home, "B-1")

    rows = ticket_rows(home)
    keys = [r[0] for r in rows]
    assert keys.index("B-1") < keys.index("A-1"), "implementing should sort before triaging"


# --- TUI-8: filtered views / needs-you queue ---------------------------------

_NEEDS_YOU_PHASES = frozenset({Phase.AWAITING_HUMAN, Phase.DEGRADED})


def _make_ticket_at_phase(home, key, title, phase):
    """Create a ticket and set it to the given phase."""
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 1\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": title, "source": "test", "spec_hash": "x"}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase, "reason": ""}, actor="r")
    snap_mod.rebuild(home, key)


def test_ticket_rows_done_phase_ordered_by_updated_ts_desc(home):
    """Within the done group, most-recently-updated tickets sort first."""
    for key, ts in (("A-1", "2026-07-01T08:00:00+00:00"),
                    ("A-2", "2026-07-01T10:00:00+00:00"),
                    ("A-3", "2026-07-01T09:00:00+00:00")):
        snap = snap_mod.Snapshot(key=key, phase=Phase.DONE.value, title=key, updated_ts=ts)
        store.write_json(store.snapshot_path(home, key), snap.to_dict())

    rows = ticket_rows(home)
    keys = [r[0] for r in rows]
    assert keys == ["A-2", "A-3", "A-1"], "done rows must be newest-updated-first"


def test_ticket_rows_filter_needs_you(home):
    """needs-you filter returns only awaiting-human and degraded tickets."""
    _make_ticket_at_phase(home, "A-1", "awaiting", "awaiting-human")
    _make_ticket_at_phase(home, "A-2", "degraded", "degraded")
    _make_ticket_at_phase(home, "A-3", "implementing", "implementing")
    _make_ticket_at_phase(home, "A-4", "ready", "ready")

    rows = ticket_rows(home, _NEEDS_YOU_PHASES)
    keys = {r[0] for r in rows}
    assert keys == {"A-1", "A-2"}


def test_ticket_rows_filter_needs_you_matches_cmd_status(home):
    """Needs-you TUI filter uses the same phase set that cmd_status uses for needs_you."""
    cmd_status_phases = {Phase.AWAITING_HUMAN.value, Phase.DEGRADED.value}
    tui_phases = {p.value for p in _NEEDS_YOU_PHASES}
    assert cmd_status_phases == tui_phases, (
        "TUI needs-you filter must match cmd_status needs_you phase set"
    )

    _make_ticket_at_phase(home, "A-1", "awaiting", "awaiting-human")
    _make_ticket_at_phase(home, "A-2", "implementing", "implementing")

    rows = ticket_rows(home, _NEEDS_YOU_PHASES)
    assert len(rows) == 1
    assert rows[0][0] == "A-1"


def test_ticket_rows_filter_active(home):
    """active filter excludes sleeping (awaiting-human, awaiting-ci) and done tickets."""
    _make_ticket_at_phase(home, "A-1", "implementing", "implementing")
    _make_ticket_at_phase(home, "A-2", "awaiting-human", "awaiting-human")
    _make_ticket_at_phase(home, "A-3", "awaiting-ci", "awaiting-ci")
    _make_ticket_at_phase(home, "A-4", "done", "done")

    rows = ticket_rows(home, ACTIVE_PHASES)
    keys = {r[0] for r in rows}
    assert "A-1" in keys
    assert "A-2" not in keys
    assert "A-3" not in keys
    assert "A-4" not in keys


def test_ticket_rows_filter_none_returns_all(home):
    """ticket_rows with phases=None returns all tickets regardless of phase."""
    _make_ticket_at_phase(home, "A-1", "awaiting", "awaiting-human")
    _make_ticket_at_phase(home, "A-2", "implementing", "implementing")
    _make_ticket_at_phase(home, "A-3", "done", "done")

    rows = ticket_rows(home)
    assert len(rows) == 3


def test_ticket_rows_filter_empty_phases(home):
    """ticket_rows with an empty frozenset returns no rows."""
    _make_ticket_at_phase(home, "A-1", "implementing", "implementing")

    rows = ticket_rows(home, frozenset())
    assert rows == []


# --- detail pane rendering ---------------------------------------------------

from maestro.tui.detail import render as _render_detail  # noqa: E402


def test_render_detail_all_fields_present():
    """All snapshot fields appear in the detail output; tier is passed in by the
    caller (dispatcher.spec_tier), not read off the snapshot."""
    snap = snap_mod.Snapshot(
        key="T-1",
        phase="implementing",
        title="My feature",
        source="inbox/_new",
        pr_number=7,
        pr_url="https://github.com/x/y/pull/7",
        pr_state="open",
        pr_draft=True,
        ci_state="passing",
        failure_count=2,
        last_error="boom",
        open_questions={"q1": "Is this OK?"},
        updated_ts="2026-06-25T00:00:00+00:00",
    )
    out = _render_detail(snap, tier=1)
    assert "My feature" in out
    assert "implementing" in out
    assert "1" in out          # tier
    assert "inbox/_new" in out
    assert "#7" in out
    assert "open" in out
    assert "passing" in out
    assert "2" in out          # failure_count
    assert "boom" in out
    assert "Is this OK?" in out
    assert "2026-06-25" in out


def test_render_detail_tier_zero_renders_as_zero_not_emdash():
    """The falsy-0 bug: tier 0 (auto-approved) must render '0', never '—'."""
    snap = snap_mod.Snapshot(key="T-1", phase="ready")
    out = _render_detail(snap, tier=0)
    lines = [l for l in out.splitlines() if "Tier" in l]
    assert lines
    assert "0" in lines[0]
    assert "—" not in lines[0]


def test_render_detail_missing_values_show_emdash():
    """None / empty fields render as em-dash."""
    snap = snap_mod.Snapshot(key="T-99")
    out = _render_detail(snap)
    # At least tier, source, PR, CI, last_error should all show —
    assert out.count("—") >= 5


def test_render_detail_no_pr_shows_emdash():
    """When no PR exists the PR line shows em-dash."""
    snap = snap_mod.Snapshot(key="T-2", phase="ready")
    out = _render_detail(snap)
    lines = [l for l in out.splitlines() if "PR" in l]
    assert lines, "Expected a PR line"
    assert "—" in lines[0]


def test_render_detail_open_questions_rendered():
    """Multiple open questions are all included in the output."""
    snap = snap_mod.Snapshot(
        key="T-3",
        open_questions={"q1": "First question", "q2": "Second question"},
    )
    out = _render_detail(snap)
    assert "First question" in out
    assert "Second question" in out


def test_render_detail_no_open_questions_shows_emdash():
    """Empty open_questions dict renders as em-dash."""
    snap = snap_mod.Snapshot(key="T-4", open_questions={})
    out = _render_detail(snap)
    lines = [l for l in out.splitlines() if "Open questions" in l or "questions" in l.lower()]
    assert any("—" in l for l in lines)


# --- Runner row (UX-2) --------------------------------------------------------

def test_render_detail_runner_row_defaults_to_claude():
    """No spec override (runner=None) renders the board default, not an em-dash --
    an absent override is a normal, common state."""
    snap = snap_mod.Snapshot(key="T-1", phase="ready")
    out = _render_detail(snap)
    lines = [l for l in out.splitlines() if "Runner" in l]
    assert lines
    assert "claude" in lines[0]
    assert "—" not in lines[0]


def test_render_detail_runner_row_shows_override_and_model():
    snap = snap_mod.Snapshot(key="T-1", phase="ready")
    out = _render_detail(snap, runner="opencode", runner_model="qwen3-coder:30b")
    lines = [l for l in out.splitlines() if "Runner" in l]
    assert lines
    assert "opencode" in lines[0]
    assert "qwen3-coder:30b" in lines[0]


# --- detail markup is valid for every phase (regression: awaiting-ci crashed) ----

import pytest  # noqa: E402
from textual.content import Content  # noqa: E402


def _assert_valid_markup(markup: str) -> None:
    """Mirror Static.update(): malformed markup raises here, as it did in the UI."""
    Content.from_markup(markup)


@pytest.mark.parametrize("phase", [p.value for p in Phase])
def test_render_detail_valid_markup_with_pr_every_phase(phase):
    """Every phase renders parseable markup when a PR is present.

    awaiting-ci tickets always carry a pr_url; the unquoted [link=URL] markup used
    to crash the detail pane the moment the cursor landed on such a ticket.
    """
    snap = snap_mod.Snapshot(
        key="T-1",
        phase=phase,
        title="My feature",
        source="inbox/_new",
        pr_number=15,
        pr_url="https://github.com/cortop/maestro/pull/15",
        pr_state="open",
        pr_draft=True,
        ci_state="passing",
        failure_count=1,
        last_error="boom",
        open_questions={"q1": "Is this OK?"},
        updated_ts="2026-06-25T00:00:00+00:00",
    )
    _assert_valid_markup(_render_detail(snap, tier=1))


@pytest.mark.parametrize("phase", [p.value for p in Phase])
def test_render_detail_valid_markup_minimal_every_phase(phase):
    """A bare snapshot (no PR, no questions) also yields parseable markup per phase."""
    _assert_valid_markup(_render_detail(snap_mod.Snapshot(key="T-2", phase=phase)))


def test_render_detail_escapes_brackets_in_dynamic_fields():
    """Bracketed content in title/last_error/questions must not break the markup."""
    snap = snap_mod.Snapshot(
        key="T-3",
        phase="degraded",
        title="fix [urgent] thing",
        last_error="Traceback: KeyError['nope'] at [line 5]",
        open_questions={"q1": "use [a] or [b]?"},
    )
    out = _render_detail(snap)
    _assert_valid_markup(out)
    # content survives (sans the escaping backslash)
    assert "[urgent]" in out.replace("\\", "")
    assert "KeyError['nope']" in out.replace("\\", "")


def test_render_detail_round_question_legible_and_markup_safe(home):
    """T-25 AC1/AC4: a real `maestro ask` round's numbered/recommended text renders
    with the round position pulled out (not squashed into the raw '1/2. ...' string)
    and the recommendation on its own line -- and stays valid, uncorrupted markup
    even when the question and recommendation both carry literal '[...]' (the
    historical crasher for this pane)."""
    store.atomic_write(store.spec_path(home, "T-7"), "# T-7\napproval_tier: 1\n")
    event_log.append(home, "T-7", "TicketCreated", {"title": "T-7"}, actor="d")
    snap_mod.rebuild(home, "T-7")
    rc = main([
        "--home", str(home), "ask", "T-7",
        "--question", "Use [Foo] or [Bar]?", "go with [Foo]", "",
        "--question", "Ship it now?", "", "",
    ])
    assert rc == 0

    snap = snap_mod.load(home, "T-7")
    out = _render_detail(snap)
    _assert_valid_markup(out)

    unescaped = out.replace("\\", "")
    assert "1/2." not in unescaped, "raw round prefix leaked into the pane unparsed"
    assert "(1/2)" in unescaped  # round position rendered legibly instead
    assert "(2/2)" in unescaped
    assert "Use [Foo] or [Bar]?" in unescaped
    assert "Recommended:" in unescaped
    assert "go with [Foo]" in unescaped
    # the no-recommendation question carries no stray "Recommended:" of its own
    assert unescaped.count("Recommended:") == 1


# --- 'a' / answer modal (data-layer tests, no textual event loop required) -------

from maestro.tui import MaestroTUI, _AnswerModal, _CmdModal, _CreateModal, _FILTERS  # noqa: E402


def _make_ticket_with_questions(home, key, questions: dict):
    """Create a ticket snapshot with open questions via events."""
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 1\n")
    event_log.append(home, key, "TicketCreated", {"title": key}, actor="d")
    for qid, text in questions.items():
        event_log.append(home, key, "QuestionAsked", {"qid": qid, "text": text}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": "awaiting-human", "reason": ""}, actor="d")
    snap_mod.rebuild(home, key)


def _make_app_with_mocked_screen(home):
    """Return (app, push_calls) where push_calls captures (screen, callback) tuples."""
    app = MaestroTUI(home=str(home))
    push_calls = []

    def fake_push_screen(screen, callback=None):
        push_calls.append((screen, callback))

    app.push_screen = fake_push_screen
    app.notify = mock.Mock()
    app.query_one = mock.Mock(return_value=mock.Mock())
    return app, push_calls


def test_answer_action_appends_inbox_command(home):
    """action_answer for a single-question ticket appends the correct ans command."""
    _make_ticket_with_questions(home, "T-1", {"q1": "Shall I proceed?"})
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_answer()

    assert len(push_calls) == 1
    screen, callback = push_calls[0]
    assert isinstance(screen, _AnswerModal)

    # Simulate user submitting the answer
    callback("Yes, go ahead")

    pending = inbox.pending(home, "T-1")
    assert len(pending) == 1
    assert pending[0]["command"] == "ans"
    assert pending[0]["args"]["qid"] == "q1"
    assert pending[0]["args"]["text"] == "Yes, go ahead"


def test_answer_cancel_leaves_state_untouched(home):
    """Dismissing the modal with None (cancel) appends nothing to the inbox."""
    _make_ticket_with_questions(home, "T-1", {"q1": "Ready?"})
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_answer()
    _, callback = push_calls[0]
    callback(None)  # simulate Escape / cancel

    assert not inbox.pending(home, "T-1")


def test_answer_walks_multiple_questions_one_at_a_time(home):
    """Two open questions surface two sequential modals; both answers are queued."""
    _make_ticket_with_questions(home, "T-1", {"q1": "First?", "q2": "Second?"})
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_answer()

    # First modal presented
    assert len(push_calls) == 1
    _, cb1 = push_calls[0]
    cb1("answer-one")  # submit first answer → triggers second modal

    # Second modal presented
    assert len(push_calls) == 2
    _, cb2 = push_calls[1]
    cb2("answer-two")  # submit second answer

    pending = inbox.pending(home, "T-1")
    assert len(pending) == 2
    answers = {p["args"]["qid"]: p["args"]["text"] for p in pending}
    assert answers["q1"] == "answer-one"
    assert answers["q2"] == "answer-two"


def test_answer_cancel_mid_walk_stops_at_that_question(home):
    """Cancelling on the second question leaves only the first answer queued."""
    _make_ticket_with_questions(home, "T-1", {"q1": "First?", "q2": "Second?"})
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_answer()
    _, cb1 = push_calls[0]
    cb1("answer-one")  # submit first → second modal opens

    assert len(push_calls) == 2
    _, cb2 = push_calls[1]
    cb2(None)  # cancel second → walk stops

    # No third modal
    assert len(push_calls) == 2

    pending = inbox.pending(home, "T-1")
    assert len(pending) == 1
    assert pending[0]["args"]["qid"] == "q1"


def test_tui_default_filter_is_needs_you():
    """MaestroTUI starts with filter index 0 which is 'needs-you'."""
    app = MaestroTUI(home="/tmp")
    assert app._filter_idx == 0
    name, phases = _FILTERS[0]
    assert name == "needs-you"


def test_tui_cycle_filter_advances_and_wraps():
    """action_cycle_filter increments the index and wraps back to 0."""
    app = MaestroTUI(home="/tmp")
    app._populate = mock.Mock()

    assert app._filter_idx == 0
    app.action_cycle_filter()
    assert app._filter_idx == 1
    app.action_cycle_filter()
    assert app._filter_idx == 2
    app.action_cycle_filter()
    assert app._filter_idx == 0  # wraps around


def test_tui_cycle_filter_calls_populate():
    """action_cycle_filter always triggers a repopulate."""
    app = MaestroTUI(home="/tmp")
    populate_mock = mock.Mock()
    app._populate = populate_mock

    app.action_cycle_filter()
    populate_mock.assert_called_once()


def test_answer_modal_receives_home(home):
    """_AnswerModal is created with the home path so it can load the spec."""
    _make_ticket_with_questions(home, "T-1", {"q1": "Ready?"})
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_answer()

    assert len(push_calls) == 1
    screen, _ = push_calls[0]
    assert isinstance(screen, _AnswerModal)
    assert screen._home == home


def test_answer_modal_spec_loaded_from_disk(home):
    """The spec file exists at the path _AnswerModal will read it from."""
    _make_ticket_with_questions(home, "T-1", {"q1": "OK?"})
    spec_content = "# T-1\napproval_tier: 1\n\n## Intent\nDo the thing."
    store.atomic_write(store.spec_path(home, "T-1"), spec_content)
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_answer()

    screen, _ = push_calls[0]
    spec_path = home / "tickets" / "T-1" / "spec.md"
    assert spec_path.exists()
    assert spec_path.read_text() == spec_content
    assert screen._question_text == "OK?"


def test_answer_no_questions_notifies_warning(home):
    """action_answer on a ticket with no open questions shows a warning toast."""
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: 0\n")
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    snap_mod.rebuild(home, "T-1")

    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_answer()

    assert not push_calls
    app.notify.assert_called_once()
    _, kwargs = app.notify.call_args
    assert kwargs.get("severity") == "warning"


# --- 'c' / cmd modal (action_cmd, action_retry, action_discard) ---------------

def _make_degraded_ticket(home, key):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 1\n")
    event_log.append(home, key, "TicketCreated", {"title": key}, actor="d")
    event_log.append(home, key, "Stalled", {"reason": "too many failures"}, actor="r")
    snap_mod.rebuild(home, key)
    assert snap_mod.load(home, key).phase == Phase.DEGRADED.value


def test_cmd_action_opens_modal(home):
    """action_cmd pushes a _CmdModal for the selected ticket."""
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    snap_mod.rebuild(home, "T-1")

    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_cmd()

    assert len(push_calls) == 1
    screen, _ = push_calls[0]
    assert isinstance(screen, _CmdModal)


def test_cmd_action_no_selection_warns(home):
    """action_cmd with no selected ticket shows a warning and pushes no modal."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = None

    app.action_cmd()

    assert not push_calls
    app.notify.assert_called_once()
    _, kwargs = app.notify.call_args
    assert kwargs.get("severity") == "warning"


def test_cmd_modal_submit_appends_command(home):
    """Submitting the modal with a command appends it to the ticket inbox."""
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    snap_mod.rebuild(home, "T-1")

    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_cmd()
    _, callback = push_calls[0]
    callback(("my-cmd", "some args"))  # simulate modal dismiss with (command, args_text)

    pending = inbox.pending(home, "T-1")
    assert len(pending) == 1
    assert pending[0]["command"] == "my-cmd"
    assert pending[0]["args"]["text"] == "some args"


def test_cmd_modal_submit_no_args_sends_empty_args(home):
    """Submitting without args text passes an empty args dict."""
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    snap_mod.rebuild(home, "T-1")

    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_cmd()
    _, callback = push_calls[0]
    callback(("retry", ""))  # empty args text

    pending = inbox.pending(home, "T-1")
    assert pending[0]["command"] == "retry"
    assert pending[0]["args"] == {}


def test_cmd_modal_cancel_appends_nothing(home):
    """Dismissing the modal with None (cancel) does not touch the inbox."""
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    snap_mod.rebuild(home, "T-1")

    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_cmd()
    _, callback = push_calls[0]
    callback(None)

    assert not inbox.pending(home, "T-1")


def test_retry_degraded_appends_retry_command(home):
    """action_retry on a degraded ticket appends 'retry' to the inbox."""
    _make_degraded_ticket(home, "T-1")

    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_retry()

    assert not push_calls  # no modal — direct send
    pending = inbox.pending(home, "T-1")
    assert len(pending) == 1
    assert pending[0]["command"] == "retry"


def test_discard_degraded_appends_discard_command(home):
    """action_discard on a degraded ticket appends 'discard' to the inbox."""
    _make_degraded_ticket(home, "T-1")

    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_discard()

    pending = inbox.pending(home, "T-1")
    assert len(pending) == 1
    assert pending[0]["command"] == "discard"


def test_retry_non_degraded_shows_warning(home):
    """action_retry on a non-degraded ticket shows a warning and sends nothing."""
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    snap_mod.rebuild(home, "T-1")

    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_retry()

    assert not inbox.pending(home, "T-1")
    app.notify.assert_called_once()
    _, kwargs = app.notify.call_args
    assert kwargs.get("severity") == "warning"


def test_discard_non_degraded_shows_warning(home):
    """action_discard on a non-degraded ticket shows a warning and sends nothing."""
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    snap_mod.rebuild(home, "T-1")

    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_discard()

    assert not inbox.pending(home, "T-1")
    app.notify.assert_called_once()
    _, kwargs = app.notify.call_args
    assert kwargs.get("severity") == "warning"


def test_cmd_modal_degraded_phase_is_stored(home):
    """_CmdModal is created with the ticket's current phase."""
    _make_degraded_ticket(home, "T-1")

    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_cmd()

    screen, _ = push_calls[0]
    assert isinstance(screen, _CmdModal)
    assert screen._phase == Phase.DEGRADED.value


# --- fleet panel (unit-level, no Textual event loop) --------------------------

from maestro.tui import FleetScreen, _IntervalModal, _fmt_age, _render_badge, _render_fleet  # noqa: E402


def test_render_badge_up_shows_interval_and_heartbeat():
    out = _render_badge({"loaded": True, "interval": 300, "heartbeat_age_s": 12})
    assert "up" in out
    assert "green" in out
    assert "300s" in out
    assert "12s ago" in out


def test_render_badge_down_shows_dashes():
    out = _render_badge({"loaded": False, "interval": None, "heartbeat_age_s": None})
    assert "down" in out
    assert "red" in out
    assert "—" in out
    assert "never" in out


def test_fmt_age_none_returns_never():
    assert _fmt_age(None) == "never"


def test_fmt_age_seconds():
    assert _fmt_age(45) == "45s ago"


def test_fmt_age_minutes():
    assert _fmt_age(125) == "2m ago"


def test_fmt_age_hours():
    assert _fmt_age(7200) == "2h ago"


def test_render_fleet_loaded_shows_green(home):
    status = {"loaded": True, "heartbeat_age_s": 30, "interval": 300,
               "label": "com.maestro.dispatcher"}
    doctor = {"dead_letters": [], "stale": False}
    out = _render_fleet(status, doctor)
    assert "green" in out
    assert "300s" in out
    assert "com.maestro.dispatcher" in out
    assert "30s ago" in out


def test_render_fleet_not_loaded_shows_red(home):
    status = {"loaded": False, "heartbeat_age_s": None, "interval": None, "label": "x"}
    doctor = {"dead_letters": [], "stale": False}
    out = _render_fleet(status, doctor)
    assert "red" in out
    assert "never" in out


def test_render_fleet_stale_shows_yellow(home):
    status = {"loaded": True, "heartbeat_age_s": 9000, "interval": 300, "label": "x"}
    doctor = {"dead_letters": ["DEAD-1"], "stale": True}
    out = _render_fleet(status, doctor)
    assert "yellow" in out
    assert "DEAD-1" in out


def test_render_fleet_no_dead_letters_shows_emdash(home):
    status = {"loaded": False, "heartbeat_age_s": None, "interval": None, "label": "x"}
    doctor = {"dead_letters": [], "stale": False}
    out = _render_fleet(status, doctor)
    assert "—" in out


def test_render_fleet_unpaused_shows_no_rate_limit_line(home):
    status = {"loaded": True, "heartbeat_age_s": 30, "interval": 300, "label": "x"}
    doctor = {"dead_letters": [], "stale": False, "rate_limit": {"paused": False}}
    out = _render_fleet(status, doctor)
    assert "paused until" not in out


def test_render_fleet_paused_shows_paused_until_line(home):
    import time as time_mod

    until_ts = time_mod.time() + 3600
    status = {"loaded": True, "heartbeat_age_s": 30, "interval": 300, "label": "x"}
    doctor = {"dead_letters": [], "stale": False,
              "rate_limit": {"paused": True, "paused_until": until_ts}}
    out = _render_fleet(status, doctor)
    assert "paused until" in out
    until_str = time_mod.strftime("%H:%M", time_mod.localtime(until_ts))
    assert until_str in out


def test_fleet_screen_constructs(home):
    """FleetScreen can be instantiated without crashing."""
    screen = FleetScreen(home)
    assert screen._home == home


def test_maestro_tui_has_fleet_binding(home):
    """MaestroTUI binds 'F' -> fleet_panel (capital), and 'f' -> cycle_filter."""
    from maestro.tui import MaestroTUI
    app = MaestroTUI(home=str(home))
    actions = {(b.key if hasattr(b, "key") else b[0]): (b.action if hasattr(b, "action") else b[1]) for b in app.BINDINGS}
    assert actions.get("F") == "fleet_panel"
    assert actions.get("f") == "cycle_filter"


def test_fleet_screen_load_status_returns_status_and_doctor(home):
    """_load_status() returns (status_dict, doctor_dict) without launchctl."""
    screen = FleetScreen(home)

    def fake_launchctl(cmd, **kw):
        class P:
            returncode = 0
            stdout = ""
        return P()

    with mock.patch("subprocess.run", fake_launchctl):
        status, doctor = screen._load_status()

    assert "loaded" in status
    assert "dead_letters" in doctor
    assert isinstance(doctor["dead_letters"], list)


def test_fleet_screen_load_status_detects_dead_letters(home):
    """_load_status reports dead-letter tickets found in _deadletter/."""
    dl_dir = home / "tickets" / "_deadletter"
    dl_dir.mkdir(parents=True, exist_ok=True)
    (dl_dir / "DEAD-1.md").write_text("# DEAD-1")

    screen = FleetScreen(home)

    def fake_launchctl(cmd, **kw):
        class P:
            returncode = 0
            stdout = ""
        return P()

    with mock.patch("subprocess.run", fake_launchctl):
        _status, doctor = screen._load_status()

    assert "DEAD-1" in doctor["dead_letters"]


def test_fleet_screen_stale_when_old_heartbeat(home):
    """_load_status marks stale=True when heartbeat is older than 1800s."""
    store.write_json(home / "derived" / ".heartbeat.json",
                     {"epoch": store.now_epoch() - 2000})

    screen = FleetScreen(home)

    def fake_launchctl(cmd, **kw):
        class P:
            returncode = 0
            stdout = ""
        return P()

    with mock.patch("subprocess.run", fake_launchctl):
        _status, doctor = screen._load_status()

    assert doctor["stale"] is True


def test_fleet_screen_not_stale_with_recent_heartbeat(home):
    """_load_status marks stale=False when heartbeat is recent."""
    store.write_json(home / "derived" / ".heartbeat.json",
                     {"epoch": store.now_epoch() - 60})

    screen = FleetScreen(home)

    def fake_launchctl(cmd, **kw):
        class P:
            returncode = 0
            stdout = ""
        return P()

    with mock.patch("subprocess.run", fake_launchctl):
        _status, doctor = screen._load_status()

    assert doctor["stale"] is False


# --- event timeline rendering -------------------------------------------------

from maestro.tui.events import render_event, render_log, render_log_line  # noqa: E402
from maestro import event_log  # noqa: E402 (already imported above but make dep explicit)


def test_render_event_includes_seq_ts_type_actor():
    """render_event includes seq, truncated ts, type, and actor."""
    ev = {
        "seq": 3,
        "ts": "2026-06-25T10:00:00+00:00",
        "type": "PhaseChanged",
        "actor": "reconciler",
        "payload": {"phase": "implementing"},
    }
    line = render_event(ev)
    assert "3" in line
    assert "2026-06-25T10:00:00" in line
    assert "PhaseChanged" in line
    assert "reconciler" in line


def test_render_event_payload_summary_limited_to_three_keys():
    """Payload summary includes at most 3 key=value pairs."""
    ev = {
        "seq": 1,
        "ts": "2026-06-25T00:00:00+00:00",
        "type": "X",
        "actor": "a",
        "payload": {"a": 1, "b": 2, "c": 3, "d": 4},
    }
    line = render_event(ev)
    # Only first 3 keys from payload
    assert line.count("=") <= 3


def test_render_event_empty_payload():
    """Empty payload renders without crashing."""
    ev = {"seq": 1, "ts": "2026-06-25T00:00:00", "type": "T", "actor": "a", "payload": {}}
    line = render_event(ev)
    assert "T" in line


def test_render_log_order_oldest_first(home):
    """render_log returns events oldest-first (seq ascending)."""
    event_log.append(home, "T-1", "TicketCreated", {"title": "x"}, actor="d", step_id="tc-1")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": "triaging"}, actor="r", step_id="pc-1")
    from maestro import event_log as el
    evs = el.read(home, "T-1")
    lines = render_log(evs)
    assert len(lines) == 2
    # seq 1 before seq 2
    assert "1" in lines[0]
    assert "2" in lines[1]


def test_render_log_tail_limits_to_last_n(home):
    """tail=True limits output to the last _TAIL_N events."""
    from maestro.tui.events import _TAIL_N
    from maestro import event_log as el
    # Write more than _TAIL_N events
    for i in range(_TAIL_N + 5):
        el.append(home, "T-1", "Ping", {}, actor="t", step_id=f"ping-{i}")
    evs = el.read(home, "T-1")
    lines_full = render_log(evs, tail=False)
    lines_tail = render_log(evs, tail=True)
    assert len(lines_full) == _TAIL_N + 5
    assert len(lines_tail) == _TAIL_N


def test_render_log_tail_false_shows_all(home):
    """tail=False shows every event regardless of count."""
    from maestro import event_log as el
    for i in range(5):
        el.append(home, "T-1", "Ping", {}, actor="t", step_id=f"p-{i}")
    evs = el.read(home, "T-1")
    lines = render_log(evs, tail=False)
    assert len(lines) == 5


# --- cursor preservation across _populate ------------------------------------

def _make_tickets(home, keys):
    """Create minimal tickets so ticket_rows returns one row per key."""
    for key in keys:
        store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
        event_log.append(home, key, "TicketCreated", {"title": key}, actor="d")
        snap_mod.rebuild(home, key)


class _FakeRowKey:
    def __init__(self, value):
        self.value = value


class _FakeDataTable:
    """Minimal DataTable stand-in to test _populate cursor logic without Textual."""

    def __init__(self):
        self._rows: list[str] = []
        self.cursor_row = 0
        self._cursor_key: str | None = None

    @property
    def row_count(self):
        return len(self._rows)

    @property
    def cursor_row_key(self):
        if self._cursor_key is None:
            return None
        return _FakeRowKey(self._cursor_key)

    def clear(self):
        self._rows = []
        self._cursor_key = None

    def add_row(self, *cells, key=None):
        self._rows.append(key)

    def move_cursor(self, *, row):
        self.cursor_row = max(0, min(row, len(self._rows) - 1))
        self._cursor_key = self._rows[self.cursor_row] if self._rows else None

    def update(self, *args, **kwargs):
        # Stand-in for the Static filter-bar widget that _populate also updates.
        pass


def _make_app_with_fake_table(home, table):
    app = MaestroTUI(home=str(home))
    app.query_one = mock.Mock(return_value=table)
    # Use the "all" filter so freshly-created (triaging) tickets are visible;
    # these tests exercise cursor preservation, not the needs-you filter.
    app._filter_idx = _FILTERS.index(next(f for f in _FILTERS if f[0] == "all"))
    return app


def test_populate_preserves_cursor_on_refresh(home):
    """Cursor stays on the same ticket key after _populate is called again."""
    _make_tickets(home, ["A-1", "A-2", "A-3"])
    table = _FakeDataTable()
    app = _make_app_with_fake_table(home, table)

    # First populate — cursor lands on row 0
    app._populate()
    assert table.row_count == 3

    # Simulate user moving cursor to the second row
    table.move_cursor(row=1)
    key_before = table.cursor_row_key.value

    # Refresh (same tickets) — cursor should stay on the same key
    app._populate()
    assert table.cursor_row_key.value == key_before


def test_populate_clamps_cursor_when_ticket_removed(home):
    """If the highlighted ticket disappears, cursor clamps to a valid row (no crash)."""
    _make_tickets(home, ["B-1", "B-2", "B-3"])
    table = _FakeDataTable()
    app = _make_app_with_fake_table(home, table)

    app._populate()
    # Move to last row
    table.move_cursor(row=2)
    assert table.cursor_row_key.value == "B-3"

    # Remove B-3 from the store by patching ticket_rows
    remaining = ["B-1", "B-2"]
    with mock.patch("maestro.tui.app.ticket_rows") as mock_rows:
        # ticket_rows returns (key, phase, title, pr, ci, tier, fails, row_key)
        mock_rows.return_value = [
            (k, "triaging", k, "—", "—", "—", 0, k) for k in remaining
        ]
        app._populate()

    # Cursor should be on a valid row, not crashed
    assert table.cursor_row < table.row_count
    assert table.cursor_row_key is not None


def test_populate_empty_table_no_crash(home):
    """_populate on an empty ticket list does not crash and leaves table empty."""
    table = _FakeDataTable()
    app = _make_app_with_fake_table(home, table)

    app._populate()  # no tickets exist
    assert table.row_count == 0


# --- 'n' / create modal (data-layer tests, no textual event loop required) ---


def test_create_action_pushes_create_modal(home):
    """action_create pushes a _CreateModal screen."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app.action_create()
    assert len(push_calls) == 1
    screen, _ = push_calls[0]
    assert isinstance(screen, _CreateModal)


def test_create_appends_new_entry(home):
    """Submitting the create form appends to _new.jsonl with correct fields."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app.action_create()

    _, callback = push_calls[0]
    callback({
        "title": "My new ticket",
        "prefix": "FEAT",
        "tier": 2,
        "priority": 1,
        "intent": "Do the thing",
    })

    pending = inbox.pending_new(home)
    assert len(pending) == 1
    _, entry = pending[0]
    assert entry["title"] == "My new ticket"
    assert entry["prefix"] == "FEAT"
    assert entry["key"] is None
    assert entry["args"]["approval_tier"] == 2
    assert entry["args"]["priority"] == 1
    assert entry["args"]["intent"] == "Do the thing"


def test_create_cancel_appends_nothing(home):
    """Dismissing the create modal with None appends nothing."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app.action_create()

    _, callback = push_calls[0]
    callback(None)

    assert inbox.pending_new(home) == []


def test_create_defaults_tier_and_priority(home):
    """Submitting with tier=1 and priority=3 records the correct defaults."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app.action_create()

    _, callback = push_calls[0]
    callback({"title": "Minimal ticket", "key": None, "tier": 1, "priority": 3, "intent": None})

    pending = inbox.pending_new(home)
    assert len(pending) == 1
    _, entry = pending[0]
    assert entry["args"]["approval_tier"] == 1
    assert entry["args"]["priority"] == 3
    assert entry["key"] is None
    assert "intent" not in entry["args"]


def test_create_shows_queued_toast(home):
    """Successful submission shows a toast containing 'queued'."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app.action_create()

    _, callback = push_calls[0]
    callback({"title": "Test ticket", "key": None, "tier": 1, "priority": 3, "intent": None})

    app.notify.assert_called_once()
    msg = app.notify.call_args[0][0]
    assert "queued" in msg


# ---------------------------------------------------------------------------
# Compact action
# ---------------------------------------------------------------------------

def _make_ticket(home, key, n_events=3):
    """Create a ticket with n pre-snapshot events and one post-snapshot event."""
    for i in range(n_events):
        event_log.append(home, key, "Note", {"n": i}, actor="t")
    snap_mod.rebuild(home, key)
    event_log.append(home, key, "Note", {"n": n_events}, actor="t")


def test_compact_action_pushes_confirm_modal(home):
    """action_compact for a selected ticket pushes a _ConfirmModal."""
    from maestro.tui import _ConfirmModal
    _make_ticket(home, "T-1")
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_compact()

    assert len(push_calls) == 1
    screen, _ = push_calls[0]
    assert isinstance(screen, _ConfirmModal)


def test_compact_action_no_ticket_notifies(home):
    """action_compact with no selected ticket shows a warning."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = None

    app.action_compact()

    assert len(push_calls) == 0
    app.notify.assert_called_once()
    assert app.notify.call_args[1].get("severity") == "warning"


def test_compact_action_cancel_does_nothing(home):
    """Cancelling the compact confirm modal leaves the event log unchanged."""
    _make_ticket(home, "T-1", n_events=3)
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    before = store.read_jsonl(store.events_path(home, "T-1"))
    app.action_compact()
    _, callback = push_calls[0]
    callback(False)  # cancel

    after = store.read_jsonl(store.events_path(home, "T-1"))
    assert len(after) == len(before)


def test_compact_action_confirm_reduces_active_log(home):
    """Confirming compact moves pre-snapshot events to the archive."""
    _make_ticket(home, "T-1", n_events=3)
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_compact()
    _, callback = push_calls[0]

    # Intercept the worker call to run it synchronously
    workers = []
    app.run_worker = lambda fn, **kw: workers.append(fn)
    callback(True)

    assert len(workers) == 1
    result = workers[0]()  # run the compact
    assert result["archived"] == 3
    assert result["remaining"] == 1


# ---------------------------------------------------------------------------
# Release action
# ---------------------------------------------------------------------------

def _write_claim(home, key):
    """Write a dummy claim file for key."""
    import json
    claim_dir = home / "derived" / "claims"
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / f"{key}.json").write_text(json.dumps({"pid": 99999, "key": key}))


def test_release_action_pushes_confirm_modal(home):
    """action_release for a selected ticket pushes a _ConfirmModal."""
    from maestro.tui import _ConfirmModal
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_release()

    assert len(push_calls) == 1
    screen, _ = push_calls[0]
    assert isinstance(screen, _ConfirmModal)


def test_release_action_no_ticket_notifies(home):
    """action_release with no selected ticket shows a warning."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = None

    app.action_release()

    assert len(push_calls) == 0
    app.notify.assert_called_once()
    assert app.notify.call_args[1].get("severity") == "warning"


def test_release_action_cancel_leaves_claim(home):
    """Cancelling the release confirm modal leaves the claim file intact."""
    _write_claim(home, "T-1")
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_release()
    _, callback = push_calls[0]
    callback(False)  # cancel

    assert claims.claim_path(home, "T-1").exists()


def test_release_action_confirm_clears_claim(home):
    """Confirming release removes the claim file for the ticket."""
    _write_claim(home, "T-1")
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_release()
    _, callback = push_calls[0]
    callback(True)  # confirm

    assert not claims.is_claimed(home, "T-1")
    app.notify.assert_called_once()
    msg = app.notify.call_args[0][0]
    assert "T-1" in msg


# ---------------------------------------------------------------------------
# Project rebuild action
# ---------------------------------------------------------------------------

def test_project_rebuild_action_spawns_worker(home):
    """action_project_rebuild enqueues a worker."""
    app, _ = _make_app_with_mocked_screen(home)
    workers = []
    app.run_worker = lambda fn, **kw: workers.append((fn, kw))

    app.action_project_rebuild()

    assert len(workers) == 1
    _, kw = workers[0]
    assert kw.get("thread") is True


def test_project_rebuild_worker_calls_projection(home):
    """The worker calls projection.write and returns a summary string."""
    app, _ = _make_app_with_mocked_screen(home)
    result = app._run_project()
    assert "projection files" in result


def test_maestro_tui_has_compact_release_project_bindings(home):
    """MaestroTUI exposes x/z/p bindings for compact, release, and project."""
    binding_keys = {(b.key if hasattr(b, "key") else b[0]) for b in MaestroTUI.BINDINGS}
    assert "x" in binding_keys
    assert "z" in binding_keys
    assert "p" in binding_keys


# --- 's' / spec screen (TUI-10) -----------------------------------------------

from maestro.tui import SpecScreen  # noqa: E402
from maestro.tui.detail import render_pending  # noqa: E402


def test_spec_screen_constructs(home):
    """SpecScreen can be instantiated with home and key."""
    screen = SpecScreen(home, "T-1")
    assert screen._home == home
    assert screen._key == "T-1"


# --- TUI-11: env / config viewer panel ----------------------------------------

from maestro.tui import EnvScreen, _render_env  # noqa: E402
from maestro.config import Config  # noqa: E402


def _make_cfg(home, repo_path="/repo", branch_prefix="m/", reconcile_command="/rec",
               max_concurrency=8, max_impl_turns=15,
               providers=None):
    cfg = Config(home=home)
    cfg.repo_path = repo_path
    cfg.branch_prefix = branch_prefix
    cfg.reconcile_command = reconcile_command
    cfg.max_concurrency = max_concurrency
    cfg.max_impl_turns = max_impl_turns
    if providers is not None:
        cfg.providers = providers
    return cfg


def test_render_env_shows_all_maestro_env_fields(home):
    """_render_env shows home, repo_path, branch_prefix, reconcile_command, max_concurrency, max_impl_turns, providers."""
    cfg = _make_cfg(home, providers={"tracker": "jira_cli", "vcs": "github_cli"})
    out = _render_env(cfg)
    assert str(home) in out
    assert "/repo" in out
    assert "m/" in out
    assert "/rec" in out
    assert "8" in out
    assert "15" in out
    assert "jira_cli" in out
    assert "github_cli" in out


def test_render_env_shows_config_toml_path(home):
    """_render_env includes the config.toml path."""
    cfg = _make_cfg(home)
    out = _render_env(cfg)
    assert "config.toml" in out


def test_render_env_marks_missing_config_toml(home):
    """_render_env marks config.toml as not found when it doesn't exist."""
    cfg = _make_cfg(home)
    out = _render_env(cfg)
    assert "not found" in out


def test_render_env_marks_existing_config_toml(home):
    """_render_env marks config.toml as exists when the file is present."""
    cfg = _make_cfg(home)
    (home / "config.toml").write_text("[maestro]\n")
    out = _render_env(cfg)
    assert "exists" in out
    assert "not found" not in out


def test_render_env_missing_repo_path_shows_emdash(home):
    """_render_env shows — when repo_path is None."""
    cfg = _make_cfg(home, repo_path=None)
    out = _render_env(cfg)
    assert "—" in out


def test_render_env_matches_maestro_env_fields(tmp_path):
    """_render_env includes exactly the fields that `maestro env` outputs."""
    cfg = Config(home=tmp_path)
    cfg.repo_path = "/some/repo"
    out = _render_env(cfg)
    for field in ("home", "repo_path", "branch_prefix", "reconcile_command",
                   "max_concurrency", "max_impl_turns"):
        assert field in out, f"Expected field '{field}' in env panel output"


def test_render_env_shows_board_wide_runner_default(home):
    """UX-2 AC6: EnvScreen surfaces the board-wide runner default + kill switch."""
    cfg = _make_cfg(home)
    cfg.runner = "opencode"
    cfg.runner_model = "qwen3-coder:30b"
    cfg.runner_enabled = ["claude", "opencode"]
    out = _render_env(cfg)
    assert "opencode" in out
    assert "qwen3-coder:30b" in out
    assert "runner_enabled" in out


def test_env_screen_constructs(home):
    """EnvScreen can be instantiated without crashing."""
    screen = EnvScreen(home)
    assert screen._home == home


def test_maestro_tui_has_env_binding():
    """MaestroTUI exposes the 'e' → env_panel binding."""
    app = MaestroTUI(home="/tmp")
    keys = [(b.key if hasattr(b, "key") else b[0]) for b in app.BINDINGS]
    assert "e" in keys


def test_action_env_panel_pushes_env_screen(home):
    """action_env_panel pushes an EnvScreen onto the screen stack."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app.action_env_panel()
    assert len(push_calls) == 1
    screen, _ = push_calls[0]
    assert isinstance(screen, EnvScreen)


# --- IMPL_STEP timeline rendering --------------------------------------------

def test_render_event_impl_step_shows_kind_badge():
    """ImplStepRecorded events render a kind badge instead of the raw type name."""
    ev = {
        "seq": 5,
        "ts": "2026-06-25T10:00:00+00:00",
        "type": "ImplStepRecorded",
        "actor": "reconciler",
        "payload": {"kind": "edit", "tool": "Edit", "summary": "maestro/tui.py"},
    }
    line = render_event(ev)
    assert "edit" in line
    assert "maestro/tui.py" in line
    # The raw event type should not appear (replaced by kind badge)
    assert "ImplStepRecorded" not in line


def test_render_event_impl_step_command_badge():
    """kind=command renders a 'cmd' badge."""
    ev = {
        "seq": 6, "ts": "2026-06-25T10:00:00+00:00",
        "type": "ImplStepRecorded", "actor": "reconciler",
        "payload": {"kind": "command", "tool": "Bash", "summary": "make test"},
    }
    line = render_event(ev)
    assert "cmd" in line
    assert "make test" in line


def test_render_event_impl_step_subagent_badge():
    """kind=subagent renders an 'agent' badge."""
    ev = {
        "seq": 7, "ts": "2026-06-25T10:00:00+00:00",
        "type": "ImplStepRecorded", "actor": "reconciler",
        "payload": {"kind": "subagent", "tool": "Agent", "summary": "Code review"},
    }
    line = render_event(ev)
    assert "agent" in line
    assert "Code review" in line


def test_render_event_impl_step_unknown_kind_still_renders():
    """An unrecognised kind still produces a line with the summary."""
    ev = {
        "seq": 8, "ts": "2026-06-25T10:00:00+00:00",
        "type": "ImplStepRecorded", "actor": "reconciler",
        "payload": {"kind": "future-kind", "tool": "X", "summary": "something"},
    }
    line = render_event(ev)
    assert "something" in line


def test_render_event_phase_changed_styled():
    """PhaseChanged uses a milestone color (contains 'blue' for styling)."""
    ev = {
        "seq": 1, "ts": "2026-06-25T00:00:00+00:00",
        "type": "PhaseChanged", "actor": "reconciler",
        "payload": {"phase": "implementing", "reason": "worktree ready"},
    }
    line = render_event(ev)
    assert "blue" in line
    assert "PhaseChanged" in line


def test_render_event_failed_styled_red():
    """Failed events use red milestone color."""
    ev = {
        "seq": 9, "ts": "2026-06-25T00:00:00+00:00",
        "type": "Failed", "actor": "reconciler",
        "payload": {"error": "timeout"},
    }
    line = render_event(ev)
    assert "red" in line
    assert "Failed" in line


# --- render_log_line (stream-json → Rich markup) -----------------------------

def test_render_log_line_assistant_text():
    """Text blocks in assistant messages are returned as escaped lines."""
    obj = {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "Hello world"}]
        },
    }
    lines = render_log_line(obj)
    assert lines == ["Hello world"]


def test_render_log_line_assistant_tool_use():
    """tool_use blocks render with tool name badge."""
    obj = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
        },
    }
    lines = render_log_line(obj)
    assert len(lines) == 1
    assert "Bash" in lines[0]
    assert "ls" in lines[0]


def test_render_log_line_result_success():
    """Result success renders green with duration."""
    obj = {"type": "result", "subtype": "success", "duration_ms": 1234}
    lines = render_log_line(obj)
    assert len(lines) == 1
    assert "green" in lines[0]
    assert "1234ms" in lines[0]


def test_render_log_line_result_error():
    """Result error renders red."""
    obj = {"type": "result", "subtype": "error_during_execution"}
    lines = render_log_line(obj)
    assert len(lines) == 1
    assert "red" in lines[0]


def test_render_log_line_result_rate_limited_not_green():
    """The 2026-07-19 runaway payload: subtype success, is_error true, 429 — must
    render red/rate_limited, never green success."""
    obj = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error_status": 429,
        "result": "You've hit your monthly spend limit.",
    }
    lines = render_log_line(obj)
    assert len(lines) == 1
    assert "green" not in lines[0]
    assert "429" in lines[0]
    assert "rate_limited" in lines[0]


def test_render_log_line_clean_success_still_green():
    obj = {"type": "result", "subtype": "success", "is_error": False, "api_error_status": None,
           "duration_ms": 1234}
    lines = render_log_line(obj)
    assert "green" in lines[0]
    assert "1234ms" in lines[0]


def test_render_log_line_rate_limit_event_surfaced():
    """rate_limit_event is a first-class rendered line, not silently dropped."""
    obj = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "rejected",
            "rateLimitType": "five_hour",
            "resetsAt": 1784400000,
            "overageStatus": "rejected",
        },
    }
    lines = render_log_line(obj)
    assert len(lines) == 1
    assert lines[0]
    assert "green" not in lines[0]
    assert "five_hour" in lines[0]


def test_render_log_line_unknown_type_returns_empty():
    """Unknown event types produce no lines (safe no-op)."""
    obj = {"type": "system", "session_id": "abc123"}
    lines = render_log_line(obj)
    assert lines == []


def test_render_log_line_escapes_brackets_in_text():
    """Rich markup chars in text content are escaped so they don't break rendering."""
    obj = {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "use [bold] or [dim]?"}]
        },
    }
    lines = render_log_line(obj)
    assert len(lines) == 1
    # The literal '[' should be escaped
    assert "\\[" in lines[0]


# --- LogsScreen construction and action_view_logs ----------------------------

from maestro.tui import LogsScreen  # noqa: E402


def test_logs_screen_constructs(home):
    """LogsScreen can be instantiated with home and key."""
    screen = LogsScreen(home, "T-1")
    assert screen._home == home
    assert screen._key == "T-1"


def test_spec_screen_has_edit_binding():
    """SpecScreen exposes the 'e' → edit_spec binding."""
    screen = SpecScreen(Path("/tmp"), "T-1")
    keys = [b[0] for b in screen.BINDINGS]
    assert "e" in keys


def test_maestro_tui_has_spec_binding(home):
    """MaestroTUI exposes the 's' → show_spec binding."""
    app = MaestroTUI(home=str(home))
    keys = [(b.key if hasattr(b, "key") else b[0]) for b in app.BINDINGS]
    assert "s" in keys


def test_action_show_spec_pushes_spec_screen(home):
    """action_show_spec pushes a SpecScreen with the correct home/key."""
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    snap_mod.rebuild(home, "T-1")

    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"
    app.action_show_spec()

    assert len(push_calls) == 1
    screen, _ = push_calls[0]
    assert isinstance(screen, SpecScreen)
    assert screen._key == "T-1"
    assert screen._home == home


def test_action_show_spec_no_selection_warns(home):
    """action_show_spec with no selected ticket shows a warning and pushes nothing."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = None
    app.action_show_spec()

    assert not push_calls
    app.notify.assert_called_once()
    _, kwargs = app.notify.call_args
    assert kwargs.get("severity") == "warning"


# --- render_pending -----------------------------------------------------------

def test_render_pending_empty_returns_emdash():
    """Empty command list renders as em-dash."""
    out = render_pending([])
    assert "—" in out


def test_render_pending_shows_command_name():
    """A pending command includes the command name."""
    cmds = [{"ts": "2026-06-25T00:00:00", "command": "retry", "args": {}}]
    out = render_pending(cmds)
    assert "retry" in out


def test_render_pending_shows_args():
    """A command with args includes arg key and value."""
    cmds = [{"ts": "2026-06-25T00:00:00", "command": "ans", "args": {"qid": "q1", "text": "yes"}}]
    out = render_pending(cmds)
    assert "qid" in out
    assert "q1" in out
    assert "text" in out
    assert "yes" in out


def test_render_pending_multiple_commands():
    """All commands appear when multiple are pending."""
    cmds = [
        {"ts": "2026-06-25T00:00:00", "command": "retry", "args": {}},
        {"ts": "2026-06-25T00:00:01", "command": "ans", "args": {"qid": "q1"}},
    ]
    out = render_pending(cmds)
    assert "retry" in out
    assert "ans" in out


def test_render_pending_escapes_brackets():
    """Brackets in args are escaped so the markup stays valid."""
    cmds = [{"ts": "2026-06-25T00:00:00", "command": "ans", "args": {"text": "use [a] or [b]"}}]
    out = render_pending(cmds)
    _assert_valid_markup(out)
    assert "[a]" in out.replace("\\", "")


def test_render_pending_valid_markup_empty():
    """Empty pending list yields valid markup."""
    _assert_valid_markup(render_pending([]))


def test_render_pending_valid_markup_with_commands():
    """Non-empty pending list yields valid markup."""
    cmds = [{"ts": "2026-06-25T00:00:00", "command": "retry", "args": {}}]
    _assert_valid_markup(render_pending(cmds))


def test_maestro_tui_has_logs_binding(home):
    """MaestroTUI exposes the 'l' → view_logs binding."""
    app = MaestroTUI(home=str(home))
    keys = [(b.key if hasattr(b, "key") else b[0]) for b in app.BINDINGS]
    assert "l" in keys


def test_action_view_logs_pushes_logs_screen(home):
    """action_view_logs pushes a LogsScreen for the selected ticket."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = "T-1"

    app.action_view_logs()

    assert len(push_calls) == 1
    screen, _ = push_calls[0]
    assert isinstance(screen, LogsScreen)
    assert screen._key == "T-1"


def test_action_view_logs_no_selection_does_nothing(home):
    """action_view_logs with no selected ticket pushes nothing."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app._selected_key = None

    app.action_view_logs()

    assert not push_calls
