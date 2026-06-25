"""Tests for zsh autocompletion (T-3)."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPLETION_SCRIPT = REPO_ROOT / "completions" / "_maestro"


# ---------------------------------------------------------------------------
# Completion script content checks
# ---------------------------------------------------------------------------

def test_completion_script_exists():
    assert COMPLETION_SCRIPT.exists(), "completions/_maestro must exist"


def test_completion_script_defines_main_function():
    text = COMPLETION_SCRIPT.read_text()
    assert "_maestro()" in text, "completion script must define _maestro()"


def test_completion_script_defines_key_helper():
    text = COMPLETION_SCRIPT.read_text()
    assert "_maestro_keys()" in text, "completion script must define _maestro_keys()"


def test_completion_script_has_compdef_header():
    text = COMPLETION_SCRIPT.read_text()
    assert text.startswith("#compdef maestro"), "first line must be #compdef maestro"


def test_completion_script_lists_all_subcommands():
    text = COMPLETION_SCRIPT.read_text()
    required = [
        "init", "create", "ans", "answer", "cmd", "status", "show", "logs",
        "doctor", "dispatch", "project", "env", "fleet", "snapshot", "events",
        "append", "set-phase", "ask", "fold-inbox", "inbox-ack", "observe-spec",
        "requeue", "fail", "finalize", "compact", "release", "check-conflicts",
        "fold-steps", "tui",
    ]
    for sub in required:
        assert sub in text, f"completion script must mention subcommand '{sub}'"


def test_completion_script_dynamic_keys_uses_maestro_home():
    text = COMPLETION_SCRIPT.read_text()
    assert "MAESTRO_HOME" in text, "key completion must respect MAESTRO_HOME"


def test_completion_script_create_flags():
    text = COMPLETION_SCRIPT.read_text()
    for flag in ("--key", "--tier", "--priority", "--intent", "--no-nudge"):
        assert flag in text, f"create completion must include flag '{flag}'"


def test_completion_script_logs_flags():
    text = COMPLETION_SCRIPT.read_text()
    for flag in ("--key", "--list", "--session", "--follow", "--json"):
        assert flag in text, f"logs completion must include flag '{flag}'"


def test_completion_script_set_phase_lists_phases():
    text = COMPLETION_SCRIPT.read_text()
    phases = ["triaging", "awaiting-human", "ready", "implementing",
              "awaiting-ci", "in-review", "degraded", "terminating", "done"]
    for phase in phases:
        assert phase in text, f"set-phase completion must include phase '{phase}'"


def test_completion_script_zsh_syntax(tmp_path):
    """zsh -n validates syntax without executing."""
    zsh = subprocess.run(
        ["zsh", "-n", str(COMPLETION_SCRIPT)],
        capture_output=True, text=True,
    )
    assert zsh.returncode == 0, f"zsh syntax error:\n{zsh.stderr}"


# ---------------------------------------------------------------------------
# CLI: logs --key flag
# ---------------------------------------------------------------------------

from maestro.cli import cmd_logs


class _LogsArgs:
    def __init__(self, key=None, key_flag=None, list_=False, session=None,
                 follow=False, json_=False, home=None):
        self.key = key
        self.key_flag = key_flag
        self.list = list_
        self.session = session
        self.follow = follow
        self.json = json_
        self.home = home


def test_logs_key_flag_accepted(cfg, tmp_path):
    """logs --key <KEY> must work identically to the positional arg."""
    args = _LogsArgs(key=None, key_flag="T-99", home=cfg.home)
    rc = cmd_logs(args)
    assert rc == 1  # no sessions → error 1, but key was accepted (not 2)


def test_logs_missing_key_returns_error(cfg):
    """logs with no key and no --key must return exit code 2."""
    args = _LogsArgs(key=None, key_flag=None, home=cfg.home)
    rc = cmd_logs(args)
    assert rc == 2


def test_logs_positional_key_still_works(cfg):
    """Positional key argument must continue to work."""
    args = _LogsArgs(key="T-99", key_flag=None, home=cfg.home)
    rc = cmd_logs(args)
    assert rc == 1  # no sessions → 1, not 2


# ---------------------------------------------------------------------------
# make autocomplete: install and idempotency
# ---------------------------------------------------------------------------

def test_make_autocomplete_exits_zero(tmp_path):
    """make autocomplete must install without error."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    zshrc = fake_home / ".zshrc"
    zshrc.write_text("")

    env = {**os.environ, "HOME": str(fake_home)}
    result = subprocess.run(
        ["make", "autocomplete"],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True,
        env=env,
    )
    assert result.returncode == 0, f"make autocomplete failed:\n{result.stderr}"
    installed = fake_home / ".zsh" / "completions" / "_maestro"
    assert installed.exists(), "_maestro must be copied to ~/.zsh/completions/"


def test_make_autocomplete_idempotent(tmp_path):
    """Running make autocomplete twice must not add duplicate fpath lines."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    zshrc = fake_home / ".zshrc"
    zshrc.write_text("")

    env = {**os.environ, "HOME": str(fake_home)}
    for _ in range(2):
        result = subprocess.run(
            ["make", "autocomplete"],
            cwd=str(REPO_ROOT),
            capture_output=True, text=True,
            env=env,
        )
        assert result.returncode == 0, f"make autocomplete failed:\n{result.stderr}"

    content = zshrc.read_text()
    count = content.count("fpath=($HOME/.zsh/completions")
    assert count == 1, f"fpath line added {count} times, expected exactly 1"


def test_make_autocomplete_copies_correct_script(tmp_path):
    """Installed script content must match the source."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".zshrc").write_text("")

    env = {**os.environ, "HOME": str(fake_home)}
    subprocess.run(
        ["make", "autocomplete"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
    )
    installed = fake_home / ".zsh" / "completions" / "_maestro"
    assert installed.read_text() == COMPLETION_SCRIPT.read_text()
