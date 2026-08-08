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
