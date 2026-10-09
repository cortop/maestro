"""Mounted-app tests for the answer modal (_AnswerModal) and its recommendation/multiline flows."""
from __future__ import annotations

import asyncio

from textual.widgets import TextArea

from maestro import (
    config as config_mod,
    event_log,
    inbox,
    ops as ops_mod,
    snapshot as snap_mod,
    store,
)
from maestro.cli import main as cli_main
from maestro.tui import _AnswerModal
from tui_support import _make_app, _run_modal_test


def test_answer_modal_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, "T-1", "answer", _AnswerModal)  # has open questions


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
