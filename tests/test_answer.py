"""Tests for 'maestro answer' interactive walkthrough."""
import io
import sys

import pytest

from maestro import event_log, inbox, ops, snapshot as snap_mod, store
from maestro.cli import cmd_answer
from maestro.statemachine import Phase


def _create_with_question(cfg, key, question_text, qid=None):
    store.atomic_write(store.spec_path(cfg.home, key), f"# {key}\napproval_tier: 1\n")
    event_log.append(cfg.home, key, "TicketCreated", {"title": key}, actor="d")
    ops.ask(cfg, key, question_text, qid=qid)
    snap_mod.rebuild(cfg.home, key)


class _Args:
    def __init__(self, key=None, home=None):
        self.key = key
        self.home = home


def _run_answer(cfg, key=None, stdin_text=""):
    args = _Args(key=key, home=cfg.home)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    # Patch isatty to simulate a TTY
    sys.stdin.isatty = lambda: True
    try:
        return cmd_answer(args)
    finally:
        sys.stdin = old_stdin


def test_empty_queue_friendly_message(cfg, capsys):
    rc = _run_answer(cfg)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing waiting" in out


def test_single_question_answered(cfg):
    _create_with_question(cfg, "T-1", "Can I pick this up?", qid="q1")
    _run_answer(cfg, stdin_text="yes go ahead\n")
    pending = inbox.pending(cfg.home, "T-1")
    assert len(pending) == 1
    cmd = pending[0]
    assert cmd["command"] == "ans"
    assert cmd["args"]["text"] == "yes go ahead"
    assert cmd["args"]["qid"] == "q1"


def test_skip_leaves_question_open(cfg):
    _create_with_question(cfg, "T-1", "Ready?", qid="q1")
    _run_answer(cfg, stdin_text="s\n")
    assert not inbox.pending(cfg.home, "T-1")
    assert snap_mod.load(cfg.home, "T-1").question_open


def test_multi_question_walkthrough(cfg):
    _create_with_question(cfg, "T-1", "Question A?", qid="qa")
    _create_with_question(cfg, "T-2", "Question B?", qid="qb")
    _run_answer(cfg, stdin_text="answer A\nanswer B\n")

    p1 = inbox.pending(cfg.home, "T-1")
    p2 = inbox.pending(cfg.home, "T-2")
    assert p1[0]["args"]["text"] == "answer A"
    assert p2[0]["args"]["text"] == "answer B"


def test_scoped_key_only_answers_that_ticket(cfg):
    _create_with_question(cfg, "T-1", "First?", qid="q1")
    _create_with_question(cfg, "T-2", "Second?", qid="q2")
    _run_answer(cfg, key="T-1", stdin_text="yes\n")

    assert inbox.pending(cfg.home, "T-1")
    assert not inbox.pending(cfg.home, "T-2")


def test_quit_stops_early(cfg):
    _create_with_question(cfg, "T-1", "Q1?", qid="q1")
    _create_with_question(cfg, "T-2", "Q2?", qid="q2")
    _run_answer(cfg, stdin_text="q\n")
    assert not inbox.pending(cfg.home, "T-1")
    assert not inbox.pending(cfg.home, "T-2")


def test_non_tty_exits_nonzero(cfg):
    args = _Args(home=cfg.home)
    # sys.stdin is not a tty in test context by default
    rc = cmd_answer(args)
    assert rc == 1


def test_degraded_ticket_questions_included(cfg):
    # Build a degraded ticket that has an open question by appending events directly
    # (ops.ask always transitions to awaiting-human, so we bypass it here to get
    # a snapshot where phase=degraded but open_questions is non-empty)
    store.atomic_write(store.spec_path(cfg.home, "T-1"), "# T-1\napproval_tier: 1\n")
    event_log.append(cfg.home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    event_log.append(cfg.home, "T-1", "QuestionAsked", {"qid": "q1", "text": "Should I retry?"}, actor="d")
    event_log.append(cfg.home, "T-1", "Stalled", {"reason": "too many failures"}, actor="d")
    snap_mod.rebuild(cfg.home, "T-1")
    s = snap_mod.load(cfg.home, "T-1")
    assert s.phase == Phase.DEGRADED.value
    assert s.open_questions

    _run_answer(cfg, stdin_text="yes retry\n")
    pending = inbox.pending(cfg.home, "T-1")
    assert pending[0]["args"]["text"] == "yes retry"
