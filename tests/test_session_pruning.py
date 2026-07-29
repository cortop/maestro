"""Session log retention & rotation (L-6)."""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro import claims, store
from maestro.config import Config
from maestro.ops import compact, prune_session_logs
from maestro.sessions import session_name

NOW = 1_700_000_000.0  # fixed "now" for deterministic age calculations


def _make_log(home: Path, key: str, epoch: float, fmt: str = "log") -> Path:
    """Write a zero-byte session log file with the given epoch stamped in its name."""
    session_id = f"{session_name(key)}-{epoch:.6f}"
    if fmt == "stream-json":
        path = store.session_stream_path(home, key, session_id)
    else:
        path = store.session_log_path(home, key, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _cfg(home: Path, **kwargs) -> Config:
    return Config(home=home, **kwargs)


# ---------------------------------------------------------------------------
# prune_session_logs — unconfigured: no-op
# ---------------------------------------------------------------------------

def test_prune_noop_when_unconfigured(home):
    f = _make_log(home, "T-1", NOW - 100 * 86400)
    assert prune_session_logs(_cfg(home), "T-1") == 0
    assert f.exists()


# ---------------------------------------------------------------------------
# prune by retention days
# ---------------------------------------------------------------------------

def test_prune_removes_old_logs(home):
    old = _make_log(home, "T-1", NOW - 10 * 86400)
    recent = _make_log(home, "T-1", NOW - 1 * 86400)
    with patch("maestro.store.now_epoch", return_value=NOW):
        count = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1")
    assert count == 1
    assert not old.exists()
    assert recent.exists()


def test_prune_keeps_logs_within_window(home):
    f = _make_log(home, "T-1", NOW - 3 * 86400)
    with patch("maestro.store.now_epoch", return_value=NOW):
        count = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1")
    assert count == 0
    assert f.exists()


# ---------------------------------------------------------------------------
# prune by max per ticket
# ---------------------------------------------------------------------------

def test_prune_max_per_ticket_keeps_newest(home):
    # Create 5 logs at 5-hour intervals, newest first by epoch
    files = [_make_log(home, "T-1", NOW - i * 3600) for i in range(5)]
    with patch("maestro.store.now_epoch", return_value=NOW):
        count = prune_session_logs(_cfg(home, session_log_max_per_ticket=2), "T-1")
    assert count == 3
    assert files[0].exists()   # newest
    assert files[1].exists()
    assert not files[2].exists()
    assert not files[3].exists()
    assert not files[4].exists()


def test_prune_max_noop_when_under_limit(home):
    files = [_make_log(home, "T-1", NOW - i * 3600) for i in range(3)]
    with patch("maestro.store.now_epoch", return_value=NOW):
        count = prune_session_logs(_cfg(home, session_log_max_per_ticket=5), "T-1")
    assert count == 0
    assert all(f.exists() for f in files)


# ---------------------------------------------------------------------------
# live session is never pruned
# ---------------------------------------------------------------------------

def test_prune_never_removes_live_session_by_age(home):
    old = _make_log(home, "T-1", NOW - 10 * 86400)
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1", log_path=str(old))
    with patch("maestro.store.now_epoch", return_value=NOW):
        count = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1")
    assert count == 0
    assert old.exists()


def test_prune_never_removes_live_session_by_max(home):
    files = [_make_log(home, "T-1", NOW - i * 3600) for i in range(5)]
    # Mark the oldest as the live session
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1", log_path=str(files[4]))
    with patch("maestro.store.now_epoch", return_value=NOW):
        count = prune_session_logs(_cfg(home, session_log_max_per_ticket=2), "T-1")
    # Live session (files[4]) never pruned; keep 2 non-live (files[0,1]); delete files[2,3]
    assert files[4].exists()
    assert files[0].exists()
    assert files[1].exists()
    assert not files[2].exists()
    assert not files[3].exists()
    assert count == 2


def test_prune_removes_session_whose_pid_is_dead(home):
    old = _make_log(home, "T-1", NOW - 10 * 86400)
    claims.write_claim(home, "T-1", 99999999, "reconcile-T-1", log_path=str(old))
    with patch("maestro.claims.pid_alive", return_value=False), \
         patch("maestro.store.now_epoch", return_value=NOW):
        count = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1")
    assert count == 1
    assert not old.exists()


def test_prune_removes_session_whose_claim_is_denied(home):
    """A live pid whose recorded epoch predates its true start (pid reuse) is a
    verified-denied identity, not our reconciler — its stale log is fair game
    even though the pid itself is very much alive (T-17)."""
    old = _make_log(home, "T-1", NOW - 10 * 86400)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.write_json(claims.claim_path(home, "T-1"),
                         {"pid": proc.pid, "name": "reconcile-T-1",
                          "ts": store.iso_now(), "epoch": store.now_epoch() - 3600,
                          "log_path": str(old)})
        count = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    assert count == 1
    assert not old.exists()


# ---------------------------------------------------------------------------
# compact integrates session pruning
# ---------------------------------------------------------------------------

def test_compact_returns_pruned_logs_count(home):
    from maestro import event_log, snapshot as snap_mod
    event_log.append(home, "T-1", "Note", {"n": 1}, actor="t")
    snap_mod.rebuild(home, "T-1")
    _make_log(home, "T-1", NOW - 10 * 86400)
    cfg = _cfg(home, session_log_retention_days=5)
    with patch("maestro.store.now_epoch", return_value=NOW):
        result = compact(cfg, "T-1")
    assert result["pruned_logs"] == 1


def test_compact_pruned_logs_zero_when_unconfigured(home):
    from maestro import event_log, snapshot as snap_mod
    event_log.append(home, "T-1", "Note", {"n": 1}, actor="t")
    snap_mod.rebuild(home, "T-1")
    _make_log(home, "T-1", NOW - 10 * 86400)
    result = compact(_cfg(home), "T-1")
    assert result["pruned_logs"] == 0
