"""Tests for 'maestro create' — both flag-based and interactive forms."""
import io
import sys
import types

import pytest

from maestro import inbox
from maestro.cli import cmd_create


class _Args:
    def __init__(self, title=None, key=None, tier=1, priority=3, intent=None,
                 home=None, no_nudge=True):
        self.title = title
        self.key = key
        self.tier = tier
        self.priority = priority
        self.intent = intent
        self.home = home
        self.no_nudge = no_nudge


def _run_create(cfg, title=None, key=None, tier=1, priority=3, intent=None,
                stdin_text="", fake_editor=None):
    """Helper that runs cmd_create with faked stdin and optionally a fake editor."""
    args = _Args(title=title, key=key, tier=tier, priority=priority, intent=intent, home=cfg.home)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    sys.stdin.isatty = lambda: True  # simulate TTY

    if fake_editor is not None:
        import maestro.cli as cli_mod
        old_editor_fn = cli_mod._editor_intent
        cli_mod._editor_intent = fake_editor
    try:
        return cmd_create(args)
    finally:
        sys.stdin = old_stdin
        if fake_editor is not None:
            cli_mod._editor_intent = old_editor_fn


# --- flag-based (non-interactive) form ---------------------------------------

def test_flag_form_queues_ticket(cfg):
    rc = _run_create(cfg, title="Fix the bug")
    assert rc == 0
    pending = inbox.pending_new(cfg.home)
    assert len(pending) == 1
    _, entry = pending[0]
    assert entry["title"] == "Fix the bug"


def test_flag_form_with_intent(cfg):
    rc = _run_create(cfg, title="Fix bug", intent="Some intent text")
    assert rc == 0
    _, entry = inbox.pending_new(cfg.home)[0]
    assert entry["args"]["intent"] == "Some intent text"


def test_flag_form_with_key(cfg):
    rc = _run_create(cfg, title="Fix bug", key="T-42")
    assert rc == 0
    _, entry = inbox.pending_new(cfg.home)[0]
    assert entry["key"] == "T-42"


def test_flag_form_tier_priority(cfg):
    rc = _run_create(cfg, title="Fix bug", tier=0, priority=1)
    assert rc == 0
    _, entry = inbox.pending_new(cfg.home)[0]
    assert entry["args"]["approval_tier"] == 0
    assert entry["args"]["priority"] == 1


# --- interactive form --------------------------------------------------------

def test_interactive_guided_flow(cfg):
    """Guided flow: provide title/tier/priority/key via stdin, fake editor returns intent."""
    def fake_editor(title, tier, priority):
        return "My rich intent from editor"

    # stdin: title, tier (accept default), priority (accept default), key (blank)
    rc = _run_create(cfg, stdin_text="My new ticket\n\n\n\n",
                     fake_editor=fake_editor)
    assert rc == 0
    pending = inbox.pending_new(cfg.home)
    assert len(pending) == 1
    _, entry = pending[0]
    assert entry["title"] == "My new ticket"
    assert entry["args"]["intent"] == "My rich intent from editor"
    assert entry["args"]["approval_tier"] == 1
    assert entry["args"]["priority"] == 3


def test_interactive_custom_tier_priority(cfg):
    def fake_editor(title, tier, priority):
        return "Intent"

    rc = _run_create(cfg, stdin_text="My ticket\n2\n5\n\n", fake_editor=fake_editor)
    assert rc == 0
    _, entry = inbox.pending_new(cfg.home)[0]
    assert entry["args"]["approval_tier"] == 2
    assert entry["args"]["priority"] == 5


def test_interactive_stdin_intent_fallback(cfg):
    """When no $EDITOR is available, falls back to stdin multi-line intent."""
    import maestro.cli as cli_mod
    old_editor_fn = cli_mod._editor_intent

    def fake_no_editor(title, tier, priority):
        return None  # simulate no editor

    cli_mod._editor_intent = fake_no_editor
    old_stdin = sys.stdin
    # stdin: title, tier default, priority default, key blank, then intent lines + blank
    sys.stdin = io.StringIO("Ticket title\n\n\n\nLine one of intent\nLine two\n\n")
    sys.stdin.isatty = lambda: True
    try:
        args = _Args(home=cfg.home)
        rc = cmd_create(args)
    finally:
        sys.stdin = old_stdin
        cli_mod._editor_intent = old_editor_fn

    assert rc == 0
    _, entry = inbox.pending_new(cfg.home)[0]
    assert entry["title"] == "Ticket title"
    assert "Line one of intent" in entry["args"]["intent"]


def test_interactive_abort_empty_title(cfg):
    """Empty title should abort without queuing."""
    rc = _run_create(cfg, stdin_text="\n")  # just hit enter for title
    assert rc == 0
    assert inbox.pending_new(cfg.home) == []


def test_interactive_abort_ctrl_c(cfg, monkeypatch):
    """KeyboardInterrupt during prompts aborts without queuing."""
    import maestro.cli as cli_mod
    old_prompt = cli_mod._prompt

    def raising_prompt(text, default=""):
        raise KeyboardInterrupt

    cli_mod._prompt = raising_prompt
    old_stdin = sys.stdin
    sys.stdin = io.StringIO("")
    sys.stdin.isatty = lambda: True
    try:
        args = _Args(home=cfg.home)
        rc = cmd_create(args)
    finally:
        sys.stdin = old_stdin
        cli_mod._prompt = old_prompt

    assert rc == 0
    assert inbox.pending_new(cfg.home) == []


def test_non_tty_no_title_returns_error(cfg):
    """No title + non-TTY stdin should error (not hang)."""
    args = _Args(home=cfg.home)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO("")
    # isatty() returns False by default for StringIO
    try:
        rc = cmd_create(args)
    finally:
        sys.stdin = old_stdin
    assert rc != 0
