"""Tests for 'maestro create' — both flag-based and interactive forms."""
import io
import sys
import types

import pytest

from maestro import inbox
from maestro.cli import cmd_create


class _Args:
    def __init__(self, title=None, key=None, tier=1, priority=3, intent=None,
                 home=None, no_nudge=True, kind=None, model=None, effort=None,
                 notes=None, depends_on=None, prefix=None):
        self.title = title
        self.key = key
        self.tier = tier
        self.priority = priority
        self.intent = intent
        self.home = home
        self.kind = kind
        self.model = model
        self.effort = effort
        self.notes = notes
        self.depends_on = depends_on
        self.prefix = prefix
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


# --- RT-1: new flags (kind/model/effort/notes/depends-on) ---

def _run_create_with_flags(cfg, **kwargs):
    """Run cmd_create with non-interactive flags; return (rc, inbox_entry)."""
    args = _Args(home=cfg.home, **kwargs)
    rc = cmd_create(args)
    entries = inbox.pending_new(cfg.home)
    entry = entries[-1][1] if entries else None
    return rc, entry


def test_flag_kind_stored_in_args(cfg):
    rc, entry = _run_create_with_flags(cfg, title="Research thing", kind="research")
    assert rc == 0
    assert entry["args"]["kind"] == "research"


def test_flag_model_stored_in_args(cfg):
    rc, entry = _run_create_with_flags(cfg, title="Big task", model="opus")
    assert rc == 0
    assert entry["args"]["model"] == "opus"


def test_flag_effort_stored_in_args(cfg):
    rc, entry = _run_create_with_flags(cfg, title="Intensive task", effort="high")
    assert rc == 0
    assert entry["args"]["effort"] == "high"


def test_flag_notes_stored_in_args(cfg):
    rc, entry = _run_create_with_flags(cfg, title="Task with notes", notes="Use web search")
    assert rc == 0
    assert entry["args"]["notes"] == "Use web search"


def test_flag_depends_on_stored_in_args(cfg):
    rc, entry = _run_create_with_flags(cfg, title="Dependent task", depends_on=["T-1", "T-2"])
    assert rc == 0
    assert entry["args"]["depends_on"] == ["T-1", "T-2"]


def test_create_all_new_flags_together(cfg):
    """AC3: create with all new flags writes correct front-matter via dispatch."""
    from maestro import dispatcher as disp, store
    from maestro.sessions import DryRunSessions

    rc, entry = _run_create_with_flags(
        cfg, title="Research task", key="R-1",
        kind="research", model="opus", effort="high",
        notes="Use web search.", depends_on=["T-5"],
    )
    assert rc == 0
    assert entry["args"]["kind"] == "research"
    assert entry["args"]["model"] == "opus"
    assert entry["args"]["effort"] == "high"
    assert entry["args"]["notes"] == "Use web search."
    assert entry["args"]["depends_on"] == ["T-5"]

    # Mint the ticket through a real dispatch sweep and assert the written spec.
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    spec_text = store.spec_path(cfg.home, "R-1").read_text()
    assert "kind: research" in spec_text
    assert "model: opus" in spec_text
    assert "effort: high" in spec_text
    assert "dependsOn: [T-5]" in spec_text
    assert "## Notes" in spec_text
    assert "Use web search." in spec_text


def test_create_cli_main_with_all_flags(cfg):
    """AC3 via cli.main([...]) to exercise the full parser pipeline."""
    from maestro.cli import main
    from maestro import dispatcher as disp, store
    from maestro.sessions import DryRunSessions

    rc = main([
        "--home", str(cfg.home),
        "create", "Research via CLI",
        "--key", "R-2",
        "--kind", "research",
        "--model", "opus",
        "--effort", "high",
        "--notes", "CLI-driven note",
        "--depends-on", "T-1", "T-2",
        "--no-nudge",
    ])
    assert rc == 0

    disp.dispatch(cfg, DryRunSessions(), now=1000)
    spec_text = store.spec_path(cfg.home, "R-2").read_text()
    assert "kind: research" in spec_text
    assert "model: opus" in spec_text
    assert "effort: high" in spec_text
    assert "dependsOn: [T-1, T-2]" in spec_text
    assert "## Notes" in spec_text
    assert "CLI-driven note" in spec_text
