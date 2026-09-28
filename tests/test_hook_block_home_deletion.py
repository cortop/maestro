"""Real invocations of .claude/hooks/block-home-deletion.py -- the PreToolUse hook
that blocks destructive shell commands against MAESTRO_HOME (see CLAUDE.md: "NEVER
delete the state home / event logs"). The hook is stdlib-only and has zero dependency
on the maestro package, so these tests exercise it exactly as Claude Code would: spawn
it as a subprocess and feed it a PreToolUse JSON payload on stdin.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from maestro import cli

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "block-home-deletion.py"


def _env(home):
    return {"MAESTRO_HOME": str(home), "PATH": "/usr/bin:/bin", "HOME": str(Path.home())}


def run_hook(command, cwd, home, tool_name="Bash"):
    payload = {"tool_name": tool_name, "tool_input": {"command": command}, "cwd": str(cwd)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_env(home),
    )


def test_blocks_rm_of_events_dir(home):
    result = run_hook(f"rm -rf {home}/events", cwd=home, home=home)
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


def test_blocks_bare_home_deletion(home):
    result = run_hook(f"rm -rf {home}", cwd=home, home=home)
    assert result.returncode == 2


def test_blocks_mv_of_tickets_dir(home):
    result = run_hook(f"mv {home}/tickets /tmp/elsewhere", cwd=home, home=home)
    assert result.returncode == 2


def test_blocks_truncate_of_config(home):
    result = run_hook(f"truncate -s0 {home}/config.toml", cwd=home, home=home)
    assert result.returncode == 2


def test_blocks_redirect_over_inbox(home):
    result = run_hook(f"echo x > {home}/inbox/T-1.jsonl", cwd=home, home=home)
    assert result.returncode == 2


def test_blocks_git_clean_inside_home(home):
    result = run_hook("git clean -fdx", cwd=home, home=home)
    assert result.returncode == 2


def test_blocks_via_shell_variable_expansion(home):
    result = run_hook("rm -rf $MAESTRO_HOME/events", cwd=home, home=home)
    assert result.returncode == 2


def test_blocks_via_mhome_shell_variable_expansion(home):
    """The phase skills mint $MHOME (not $MAESTRO_HOME) -- the hook must
    expand it too, or the rename silently blinds this guard."""
    result = run_hook("rm -rf $MHOME/events", cwd=home, home=home)
    assert result.returncode == 2
    assert str(home) in result.stderr


def test_blocks_via_braced_mhome_shell_variable_expansion(home):
    result = run_hook("rm -rf ${MHOME}", cwd=home, home=home)
    assert result.returncode == 2
    assert str(home) in result.stderr


def test_blocks_bare_mhome_deletion(home):
    result = run_hook("rm -rf $MHOME", cwd=home, home=home)
    assert result.returncode == 2
    assert str(home) in result.stderr


def test_wildcard_of_unprotected_subdir_via_mhome_is_allowed(home):
    # Mirrors test_wildcard_of_unprotected_subdir_is_allowed but through the
    # $MHOME spelling the phase skills actually use.
    result = run_hook("rm -rf $MHOME/derived/snapshots/*", cwd="/tmp", home=home)
    assert result.returncode == 0


def test_blocks_absolute_path_qualified_binary(home):
    """Regression: the risky-verb regex's negative lookbehind must not exclude
    "/", or a path-qualified invocation (/bin/rm, common to bypass a `rm` shell
    alias) slips through undetected -- a real gap caught by adversarial QA
    while implementing T-21."""
    result = run_hook(f"/bin/rm -rf {home}/events", cwd=home, home=home)
    assert result.returncode == 2


def test_blocks_relative_path_qualified_binary(home):
    result = run_hook(f"bin/mv {home}/tickets /tmp/x", cwd=home, home=home)
    assert result.returncode == 2


def test_blocks_bare_rm_with_no_path_argument_at_home_root(home):
    """Regression: a bare `rm -rf` (no explicit path token) run with cwd exactly
    MAESTRO_HOME must still block -- caught by adversarial QA. The verb token
    ("rm") itself was leaking into the candidate-paths list, which made the
    "no path token -> check cwd" fallback never fire."""
    result = run_hook("rm -rf", cwd=home, home=home)
    assert result.returncode == 2


def test_blocks_wildcard_rm_at_home_root(home):
    """Regression: `rm -rf *` from MAESTRO_HOME itself is equivalent in effect
    to `rm -rf $MAESTRO_HOME` (a real shell expands "*" to everything in cwd)
    and must block, even though "*" doesn't literally equal the home path."""
    result = run_hook("rm -rf *", cwd=home, home=home)
    assert result.returncode == 2


def test_blocks_wildcard_rm_inside_protected_subdir(home):
    result = run_hook("rm -rf *", cwd=home / "events", home=home)
    assert result.returncode == 2


def test_wildcard_rm_outside_home_is_allowed(tmp_path, home):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = run_hook("rm -rf *", cwd=scratch, home=home)
    assert result.returncode == 0


def test_verb_named_argument_after_the_leading_verb_is_still_checked(home):
    """The skip_leading fix only drops the clause's *first* token when it
    equals the matched verb -- a later literal argument that happens to equal
    the verb name (e.g. deleting a file actually named "rm") must still be
    treated as a real candidate path, not silently dropped too."""
    result = run_hook("rm rm", cwd=home / "events", home=home)
    assert result.returncode == 2


def test_blocks_inside_compound_command(home):
    # The dangerous clause isn't first -- must still be caught.
    result = run_hook(f"cd /tmp && rm -rf {home}/events", cwd="/tmp", home=home)
    assert result.returncode == 2


def test_blocks_path_qualified_wildcard_at_home_root(home):
    """Regression: `rm -rf $MAESTRO_HOME/*` deletes events/tickets/inbox/
    config.toml just as thoroughly as `rm -rf $MAESTRO_HOME` -- caught by
    adversarial QA. The literal string never equals the home path, so it
    needs its trailing wildcard stripped before the exact-match check."""
    result = run_hook("rm -rf $MAESTRO_HOME/*", cwd="/tmp", home=home)
    assert result.returncode == 2


def test_blocks_path_qualified_double_star(home):
    result = run_hook("rm -rf $MAESTRO_HOME/**", cwd="/tmp", home=home)
    assert result.returncode == 2


def test_blocks_wildcard_inside_protected_subdir_by_literal_path(home):
    result = run_hook("rm -rf $MAESTRO_HOME/events/*", cwd="/tmp", home=home)
    assert result.returncode == 2


def test_wildcard_of_unprotected_subdir_is_allowed(home):
    # conftest's `home` fixture already creates derived/snapshots.
    result = run_hook("rm -rf $MAESTRO_HOME/derived/snapshots/*", cwd="/tmp", home=home)
    assert result.returncode == 0


def test_legitimate_unrelated_rm_is_allowed(home):
    """The spec's own proof obligation: a legitimate rm -rf /tmp/foo exits 0."""
    result = run_hook("rm -rf /tmp/foo", cwd="/tmp", home=home)
    assert result.returncode == 0
    assert result.stderr == ""


def test_append_redirect_is_allowed(home):
    result = run_hook(f"echo x >> {home}/events/T-1.jsonl", cwd=home, home=home)
    assert result.returncode == 0


def test_non_bash_tool_is_ignored(home):
    result = run_hook(f"rm -rf {home}/events", cwd=home, home=home, tool_name="Write")
    assert result.returncode == 0


def test_malformed_stdin_fails_open(home):
    result = subprocess.run(
        [sys.executable, str(HOOK)], input="not json", capture_output=True, text=True,
        env=_env(home),
    )
    assert result.returncode == 0


def test_relative_paths_in_a_nested_worktree_are_not_falsely_protected(home):
    """Regression for the bug found while implementing T-21: a reconciler's cwd is
    always nested under MAESTRO_HOME (worktrees live at home/worktrees/<KEY>), so a
    naive "is home an ancestor of this resolved path" check for the home-root entry
    flags *every* relative rm/mv/redirect the reconciler ever runs -- home is
    trivially an ancestor of anything resolved against a cwd that is itself under
    home. This is what caused the ticket's own watchdog "no progress" failures.
    Ordinary relative-path commands from inside a worktree must stay allowed.
    """
    worktree = home / "worktrees" / "T-99"
    worktree.mkdir(parents=True)
    for command in ("rm -rf node_modules", "mv foo.txt bar.txt", "echo hi > out.log"):
        result = run_hook(command, cwd=worktree, home=home)
        assert result.returncode == 0, f"{command!r} should not be blocked: {result.stderr}"


def test_relative_path_that_actually_resolves_into_protected_dir_is_blocked(home):
    # From MAESTRO_HOME itself (not a worktree), a relative "events/..." really
    # does resolve into the protected subtree and must still be caught.
    result = run_hook("rm -rf events/T-1.jsonl", cwd=home, home=home)
    assert result.returncode == 2


def test_git_clean_outside_home_is_allowed(home):
    result = run_hook("git clean -fdx", cwd="/tmp", home=home)
    assert result.returncode == 0


def test_hook_works_with_no_maestro_package_importable(home):
    """AC2 (T-34/RF-5): the hook -- and its extracted destructive_command_guard
    predicate module -- must keep working even with the maestro package
    unimportable and PYTHONPATH scrubbed. `-S` skips site initialization
    entirely (no site-packages, no venv activation, no .pth processing), so
    this proves zero dependency on maestro being installed, not just on
    PYTHONPATH being empty."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home())}  # note: no MAESTRO_HOME either
    payload = {"tool_name": "Bash", "tool_input": {"command": f"rm -rf {home}/events"}, "cwd": str(home)}
    result = subprocess.run(
        [sys.executable, "-S", str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True,
        env={**env, "MAESTRO_HOME": str(home)},
    )
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr

    # And a legitimate command run the same isolated way is still allowed.
    payload_ok = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/foo"}, "cwd": "/tmp"}
    result_ok = subprocess.run(
        [sys.executable, "-S", str(HOOK)],
        input=json.dumps(payload_ok), capture_output=True, text=True,
        env={**env, "MAESTRO_HOME": str(home)},
    )
    assert result_ok.returncode == 0


def test_angle_bracketed_email_in_a_commit_message_is_not_falsely_blocked(home):
    """Regression: an angle-bracketed email address (e.g. a git commit's own
    Co-Authored-By trailer) can look like a truncating redirect followed by a
    bare closing quote once the redirect regex fires on the trailing `>` --
    that stripped-to-empty "target" must never resolve to the filesystem
    root and trip the new ancestor-of-every-board-root check."""
    command = ('git commit -q -m "fix things\\n\\n'
               'Co-Authored-By: Someone <someone@example.com>"')
    result = run_hook(command, cwd="/tmp", home=home)
    assert result.returncode == 0, result.stderr


# --- T-145: parent-of-a-board protection + default-home scanning -----------
#
# These tests set HOME to a tmp dir (never the real one) so `~` and the
# default-home scan resolve entirely under it -- the real ~/.maestro is
# never touched.

@pytest.fixture
def fake_user_home(tmp_path):
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    return fake_home


def _seed_board(board_dir):
    """A real, initialized board -- `events/`+`tickets/`+`config.toml`, the
    exact shape `_looks_like_board` checks for."""
    rc = cli.main(["--home", str(board_dir), "init"])
    assert rc == 0
    return board_dir


def _run_with_env(command, cwd, env):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )


def test_blocks_ancestors_of_a_differently_named_board_with_maestro_home_set(fake_user_home):
    """AC1: MAESTRO_HOME points at a board nested two levels under the fake
    $HOME (mirrors the real ~/.maestro/maestro-dev layout) -- deleting or
    moving an ANCESTOR of it (the default home, or the user's own home) must
    still block, even though neither literal path equals MAESTRO_HOME."""
    board = _seed_board(fake_user_home / ".maestro" / "maestro-dev")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(fake_user_home), "MAESTRO_HOME": str(board)}
    for command in ("rm -rf ~/.maestro", "mv ~/.maestro ~/old", "rm -rf ~"):
        result = _run_with_env(command, cwd=board, env=env)
        assert result.returncode == 2, f"{command!r} should block: {result.stderr}"
        assert "BLOCKED by block-home-deletion hook" in result.stderr


def test_blocks_the_scanned_board_and_its_subtrees_with_maestro_home_unset(fake_user_home):
    """AC2: no MAESTRO_HOME at all (no repo chpwd hook: CI, the desktop/web
    app) -- the guard falls back to the default home, finds the real board
    nested under it, and protects that board and its subtrees, not just the
    (here, phantom) default home itself."""
    board = _seed_board(fake_user_home / ".maestro" / "maestro-dev")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(fake_user_home)}  # MAESTRO_HOME unset
    for command in (f"rm -rf {board}", f"rm -rf {board}/events", f"rm -rf {board}/tickets/T-1"):
        result = _run_with_env(command, cwd=board, env=env)
        assert result.returncode == 2, f"{command!r} should block: {result.stderr}"
    result = _run_with_env(f"echo x > {board}/config.toml", cwd=board, env=env)
    assert result.returncode == 2


@pytest.mark.parametrize("maestro_home_set", [True, False])
def test_worktree_unrelated_and_non_board_sibling_stay_allowed(fake_user_home, maestro_home_set):
    """AC3: in both environments (MAESTRO_HOME set to the board, and unset),
    the new ancestor/default-home protection must not swallow a
    reconciler's own worktree, a relative rm from inside it, an unrelated
    tmp path, or a sibling of the default home that isn't itself a board."""
    board = _seed_board(fake_user_home / ".maestro" / "maestro-dev")
    non_board_sibling = fake_user_home / ".maestro" / "notes-dir"
    non_board_sibling.mkdir(parents=True)
    (non_board_sibling / "notes.txt").write_text("hi")
    worktree = board / "worktrees" / "T-1"
    worktree.mkdir(parents=True)
    unrelated = fake_user_home / "scratch" / "unrelated"
    unrelated.mkdir(parents=True)

    env = {"PATH": "/usr/bin:/bin", "HOME": str(fake_user_home)}
    if maestro_home_set:
        env["MAESTRO_HOME"] = str(board)

    result = _run_with_env(f"rm -rf {worktree}", cwd=board, env=env)
    assert result.returncode == 0, result.stderr

    result = _run_with_env("rm -rf build", cwd=worktree, env=env)
    assert result.returncode == 0, result.stderr

    result = _run_with_env(f"rm -rf {unrelated}", cwd=fake_user_home, env=env)
    assert result.returncode == 0, result.stderr

    result = _run_with_env(f"rm -rf {non_board_sibling}", cwd=fake_user_home, env=env)
    assert result.returncode == 0, result.stderr
