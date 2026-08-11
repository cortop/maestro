"""Real invocations of .claude/hooks/guard_argv.py -- the argv-transport adapter
around destructive_command_guard.check_command for runners that don't speak Claude
Code's PreToolUse JSON-on-stdin protocol (T-34/RF-5). See that script's module
docstring for its two invocation shapes (PATH shim vs. generic ``--check`` adapter).
"""
import os
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
