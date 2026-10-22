"""Guard false positives: pi guard flag/redirect tokens, quoted `>` in the shared guard."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from maestro import dispatcher as disp
from maestro import pi_guard, store


@pytest.fixture
def checker(home):
    pi_agent_dir = store.pi_agent_dir(home)
    pi_guard.install(pi_agent_dir)
    return pi_agent_dir / "extensions" / "pi_guard_check.py"


def _run(checker, cmd, *, cwd, home):
    env = {"MAESTRO_HOME": str(home), "PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())}
    return subprocess.run([sys.executable, str(checker), "--check-bash", cmd], cwd=str(cwd),
                          capture_output=True, text=True, env=env)


def test_pi_guard_flags_and_redirects(home, checker):
    for cmd in ("maestro --help", "maestro show T-1 2>/dev/null", "maestro 2>/dev/null"):
        assert _run(checker, cmd, cwd=home, home=home).returncode == 0, cmd
    for cmd in ("maestro approve T-1", "maestro --home /x restore", "maestro -q fleet"):
        assert _run(checker, cmd, cwd=home, home=home).returncode == 2, cmd


def test_block_reason_lists_allowed_verbs(home, checker):
    result = _run(checker, "maestro approve T-1", cwd=home, home=home)
    assert result.returncode == 2
    assert "Allowed verbs:" in result.stderr
    for verb in disp.AGENT_TOOL_VERBS:
        assert verb in result.stderr


def test_quoted_redirect_not_a_write(home, checker, tmp_path):
    protected = f"{home}/events/x.jsonl"
    assert _run(checker, "sed 's/=.*/=<set>/' f", cwd=tmp_path, home=home).returncode == 0
    assert _run(checker, 'echo "a > b"', cwd=tmp_path, home=home).returncode == 0
    assert _run(checker, f"echo hi > {protected}", cwd=tmp_path, home=home).returncode == 2
    assert _run(checker, f"echo 'x' > {protected}", cwd=tmp_path, home=home).returncode == 2
    # unbalanced quote -> unparseable -> fail closed
    assert _run(checker, f'echo "x > {protected}', cwd=tmp_path, home=home).returncode == 2
