"""Real invocations of .claude/hooks/guard_argv.py -- the argv-transport adapter
around destructive_command_guard.check_command for runners that don't speak Claude
Code's PreToolUse JSON-on-stdin protocol (T-34/RF-5). See that script's module
docstring for its two invocation shapes (PATH shim vs. generic ``--check`` adapter).
"""
import subprocess
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guard_argv.py"


def _env(home, extra_path=""):
    path = f"{extra_path}:/usr/bin:/bin" if extra_path else "/usr/bin:/bin"
    return {"MAESTRO_HOME": str(home), "PATH": path, "HOME": str(Path.home())}


def run_check(command, cwd, home):
    """Generic-adapter mode: check only, nothing executed."""
    return subprocess.run(
        [sys.executable, str(ADAPTER), "--check", command],
        cwd=str(cwd), capture_output=True, text=True, env=_env(home),
    )


@pytest.mark.parametrize("command", [
    "rm -rf {home}/events",
    "/bin/rm -rf {home}/events",
    "mv {home}/tickets /tmp/elsewhere",
    "git clean -xfd",
    "> {home}/config.toml",
])
def test_argv_adapter_refuses_destructive_commands(home, command):
    (home / "config.toml").write_text("x")
    result = run_check(command.format(home=home), cwd=home, home=home)
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr
    assert home.name in result.stderr or str(home) in result.stderr
    assert (home / "events").exists()  # never actually executed


def test_argv_adapter_check_mode_allows_and_runs_nothing(tmp_path, home):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = run_check("rm -rf /tmp/nonexistent-marker-xyz", cwd=scratch, home=home)
    assert result.returncode == 0
    assert result.stderr == ""


def test_path_shim_lets_ordinary_rm_run_for_real(tmp_path, home):
    """AC4: the same shim, first on PATH under the name "rm", must let a legitimate
    `rm -rf ./build` inside a temp worktree succeed -- and the REAL rm must have run
    (the target actually gets deleted), not just "would have been allowed"."""
    shim_dir = tmp_path / "shimbin"
    shim_dir.mkdir()
    (shim_dir / "rm").symlink_to(ADAPTER)

    worktree = tmp_path / "worktree"
    build = worktree / "build"
    build.mkdir(parents=True)
    (build / "artifact.txt").write_text("x")

    result = subprocess.run(
        ["rm", "-rf", "./build"], cwd=str(worktree), capture_output=True, text=True,
        env=_env(home, extra_path=str(shim_dir)),
    )
    assert result.returncode == 0, result.stderr
    assert not build.exists()


def test_path_shim_still_refuses_destructive_command(tmp_path, home):
    """The PATH-shim mode isn't just a passthrough -- it applies the exact same
    predicate as --check mode before deciding whether to exec the real binary."""
    shim_dir = tmp_path / "shimbin"
    shim_dir.mkdir()
    (shim_dir / "rm").symlink_to(ADAPTER)

    result = subprocess.run(
        ["rm", "-rf", str(home / "events")], cwd=str(home), capture_output=True, text=True,
        env=_env(home, extra_path=str(shim_dir)),
    )
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr
    assert (home / "events").exists()


def test_generic_adapter_executes_allowed_command_for_real(tmp_path, home):
    """Without --check, an allowed command is actually run (not just approved)."""
    target = tmp_path / "out.txt"
    result = subprocess.run(
        [sys.executable, str(ADAPTER), f"echo hi > {target}"],
        cwd=str(tmp_path), capture_output=True, text=True, env=_env(home),
    )
    assert result.returncode == 0, result.stderr
    assert target.read_text().strip() == "hi"


def test_missing_command_argument_fails_closed(home):
    """Unparseable/missing argv from a wrapper we control is a bug, not untrusted
    input -- unlike the Claude hook's fail-open-on-malformed-stdin, this adapter
    fails CLOSED (non-zero, nothing executed) on a missing command argument."""
    result = subprocess.run(
        [sys.executable, str(ADAPTER)], capture_output=True, text=True, env=_env(home),
    )
    assert result.returncode != 0


# --- T-145 AC4: same allow/block verdict as the Claude hook for the parent-of-
# a-board and default-home-scanning cases (tests/test_hook_block_home_deletion.py
# carries the same scenarios against the Claude hook) --------------------------

@pytest.fixture
def fake_user_home(tmp_path):
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    return fake_home


def _seed_board(board_dir):
    from maestro import cli
    rc = cli.main(["--home", str(board_dir), "init"])
    assert rc == 0
    return board_dir


def _run_check_with_env(command, cwd, env):
    return subprocess.run(
        [sys.executable, str(ADAPTER), "--check", command],
        cwd=str(cwd), capture_output=True, text=True, env=env,
    )


def test_argv_adapter_blocks_ancestors_of_a_differently_named_board(fake_user_home):
    board = _seed_board(fake_user_home / ".maestro" / "maestro-dev")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(fake_user_home), "MAESTRO_HOME": str(board)}
    for command in ("rm -rf ~/.maestro", "mv ~/.maestro ~/old", "rm -rf ~"):
        result = _run_check_with_env(command, cwd=board, env=env)
        assert result.returncode == 2, f"{command!r} should block: {result.stderr}"
        assert "BLOCKED" in result.stderr


def test_argv_adapter_blocks_the_scanned_board_with_maestro_home_unset(fake_user_home):
    board = _seed_board(fake_user_home / ".maestro" / "maestro-dev")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(fake_user_home)}  # MAESTRO_HOME unset
    for command in (f"rm -rf {board}", f"rm -rf {board}/events", f"rm -rf {board}/tickets/T-1"):
        result = _run_check_with_env(command, cwd=board, env=env)
        assert result.returncode == 2, f"{command!r} should block: {result.stderr}"
    result = _run_check_with_env(f"> {board}/config.toml", cwd=board, env=env)
    assert result.returncode == 2


@pytest.mark.parametrize("maestro_home_set", [True, False])
def test_argv_adapter_allows_worktree_unrelated_and_non_board_sibling(fake_user_home, maestro_home_set):
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

    assert _run_check_with_env(f"rm -rf {worktree}", cwd=board, env=env).returncode == 0
    assert _run_check_with_env("rm -rf build", cwd=worktree, env=env).returncode == 0
    assert _run_check_with_env(f"rm -rf {unrelated}", cwd=fake_user_home, env=env).returncode == 0
    assert _run_check_with_env(f"rm -rf {non_board_sibling}", cwd=fake_user_home, env=env).returncode == 0
