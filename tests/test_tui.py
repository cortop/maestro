"""Tests for `maestro tui` entrypoint."""
from __future__ import annotations

import sys
from unittest import mock

from maestro import event_log, inbox, snapshot as snap_mod, store
from maestro.cli import main
from maestro.projection import ticket_rows
from maestro.statemachine import Phase


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
    assert row_key == "T-1"


def test_ticket_rows_pr_label(home):
    """A ticket with a PR shows '#<number>', one without shows '—'."""
    event_log.append(home, "T-1", "PrOpened",
                     {"number": 42, "url": "https://github.com/x/y/pull/42", "draft": True},
                     actor="r", step_id="pr-T-1")
    snap_mod.rebuild(home, "T-1")

    rows = ticket_rows(home)
    assert rows[0][3] == "#42"


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


# --- detail pane rendering ---------------------------------------------------

from maestro.tui_detail import render as _render_detail  # noqa: E402


def test_render_detail_all_fields_present():
    """All snapshot fields appear in the detail output."""
    snap = snap_mod.Snapshot(
        key="T-1",
        phase="implementing",
        title="My feature",
        tier="1",
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
    out = _render_detail(snap)
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
        tier="1",
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
    _assert_valid_markup(_render_detail(snap))


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


# --- 'a' / answer modal (data-layer tests, no textual event loop required) -------

from maestro.tui import MaestroTUI, _AnswerModal, _CreateModal  # noqa: E402


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
        "key": "FEAT-1",
        "tier": 2,
        "priority": 1,
        "intent": "Do the thing",
    })

    pending = inbox.pending_new(home)
    assert len(pending) == 1
    _, entry = pending[0]
    assert entry["title"] == "My new ticket"
    assert entry["key"] == "FEAT-1"
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
    assert entry["args"]["intent"] is None


def test_create_shows_queued_toast(home):
    """Successful submission shows a toast containing 'queued'."""
    app, push_calls = _make_app_with_mocked_screen(home)
    app.action_create()

    _, callback = push_calls[0]
    callback({"title": "Test ticket", "key": None, "tier": 1, "priority": 3, "intent": None})

    app.notify.assert_called_once()
    msg = app.notify.call_args[0][0]
    assert "queued" in msg
