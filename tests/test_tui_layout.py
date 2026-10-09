"""Layout rules for the flat `tests/test_tui_*.py` files (T-187).

Every TUI ticket used to append a section to one 5k-line file, which made it a
merge-conflict magnet. These checks keep the tests spread one-per-screen, and keep
the real-spawn guard on every TUI module without each file having to opt in.
Pure file inspection plus a subprocess pytest: needs no textual.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).parent
CORE = "test_tui_runtime.py"
MAX_FEATURE_LINES = 1500
MAX_CORE_LINES = 800


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_tui_test_files_stay_small():
    files = sorted(TESTS.glob("test_tui_*.py"))
    assert (TESTS / CORE) in files
    too_big = {
        p.name: n for p in files
        if (n := _line_count(p)) > (MAX_CORE_LINES if p.name == CORE else MAX_FEATURE_LINES)
    }
    assert not too_big, (
        f"split these by screen/modal into new tests/test_tui_<feature>.py files "
        f"(feature cap {MAX_FEATURE_LINES}, {CORE} cap {MAX_CORE_LINES}): {too_big}"
    )


def test_spawn_guard_applies_to_every_tui_module(tmp_path):
    """A `test_tui*` module with no guard of its own cannot launch a real session;
    a module outside that prefix is left alone."""
    for name in ("conftest.py", "fault_injection.py"):
        shutil.copy(TESTS / name, tmp_path / name)
    body = (
        "import pytest\n"
        "from maestro.sessions import ClaudeCliSessions, OpencodeCliSessions, PiCliSessions\n\n"
        "@pytest.mark.parametrize('cls', [ClaudeCliSessions, OpencodeCliSessions, PiCliSessions])\n"
        "def test_spawn_is_{verb}(cls):\n"
        "    {check}\n"
    )
    (tmp_path / "test_tui_probe.py").write_text(body.format(
        verb="blocked",
        check="with pytest.raises(AssertionError, match='real CLI backend spawn'):\n"
              "        cls.spawn(None)"))
    (tmp_path / "test_other_probe.py").write_text(body.format(
        verb="not_blocked", check="assert cls.spawn.__name__ == 'spawn'"))
    env = {**os.environ, "PYTHONPATH": str(TESTS.parent)}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "6 passed" in proc.stdout, proc.stdout
