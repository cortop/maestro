import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maestro.config import Config  # noqa: E402


@pytest.fixture
def home(tmp_path):
    for d in ("events", "inbox", "tickets", "worktrees", "derived/snapshots", "derived/cursors"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def cfg(home):
    return Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3)
