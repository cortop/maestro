"""Session log retention & rotation (L-6), plus the T-20 dispatcher tick."""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro import claims, store
from maestro.config import Config, load
from maestro.ops import compact, prune_session_logs
from maestro.sessions import session_name

NOW = 1_700_000_000.0  # fixed "now" for deterministic age calculations


def _make_log(home: Path, key: str, epoch: float, fmt: str = "log") -> Path:
    """Write a session log file (with a few bytes of content) with the given
    epoch stamped in its name."""
    session_id = f"{session_name(key)}-{epoch:.6f}"
    if fmt == "stream-json":
        path = store.session_stream_path(home, key, session_id)
    else:
        path = store.session_log_path(home, key, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * 10, encoding="utf-8")
    return path


def _cfg(home: Path, **kwargs) -> Config:
    return Config(home=home, **kwargs)


# ---------------------------------------------------------------------------
# config defaults (AC1)
# ---------------------------------------------------------------------------

def test_init_config_has_bounded_prune_defaults(tmp_path):
    from maestro import cli
    cli.main(["--home", str(tmp_path), "init"])
    cfg = load(str(tmp_path))
    assert cfg.prune_interval and cfg.prune_interval > 0
    assert cfg.session_log_retention_days and cfg.session_log_retention_days > 0
    assert cfg.session_log_max_per_ticket and cfg.session_log_max_per_ticket > 0


def test_preexisting_home_with_blank_maestro_block_still_bounded(tmp_path):
    """A pre-existing home (like ~/.maestro/maestro-dev) whose config.toml names
    none of the three new knobs is still protected without an edit."""
    (tmp_path / "config.toml").write_text("[maestro]\nmax_concurrency = 5\n", encoding="utf-8")
    cfg = load(str(tmp_path))
    assert cfg.prune_interval and cfg.prune_interval > 0
    assert cfg.session_log_retention_days and cfg.session_log_retention_days > 0
    assert cfg.session_log_max_per_ticket and cfg.session_log_max_per_ticket > 0


def test_explicit_zero_knobs_mean_unlimited_across_a_real_sweep(home, cfg):
    from maestro import dispatcher as disp
    from maestro.sessions import DryRunSessions

    cfg.session_log_retention_days = 0
    cfg.session_log_max_per_ticket = 0
    old = _make_log(home, "T-1", NOW - 100 * 86400)
    disp.dispatch(cfg, DryRunSessions(), now=NOW)
    assert old.exists()


# ---------------------------------------------------------------------------
# prune_session_logs — unconfigured (0/None on both knobs): no-op
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("unlimited", [0, None], ids=["zero", "none"])
def test_prune_noop_when_unconfigured(home, unlimited):
    """Both unlimited spellings (0 and None) on both knobs are a full no-op,
    even against a 100-day-old log — the non-None dataclass defaults changed the
    *default* behavior, not what an explicit unlimited opt-out means."""
    f = _make_log(home, "T-1", NOW - 100 * 86400)
    count, nbytes = prune_session_logs(
        _cfg(home, session_log_retention_days=unlimited, session_log_max_per_ticket=unlimited),
        "T-1")
    assert (count, nbytes) == (0, 0)
    assert f.exists()


# ---------------------------------------------------------------------------
# prune by retention days
# ---------------------------------------------------------------------------

def test_prune_removes_old_logs(home):
    old = _make_log(home, "T-1", NOW - 10 * 86400)
    recent = _make_log(home, "T-1", NOW - 1 * 86400)
    count, nbytes = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1", now=NOW)
    assert count == 1
    assert nbytes > 0
    assert not old.exists()
    assert recent.exists()


def test_prune_keeps_logs_within_window(home):
    f = _make_log(home, "T-1", NOW - 3 * 86400)
    count, nbytes = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1", now=NOW)
    assert (count, nbytes) == (0, 0)
    assert f.exists()


# ---------------------------------------------------------------------------
# prune by max per ticket
# ---------------------------------------------------------------------------

def test_prune_max_per_ticket_keeps_newest(home):
    # Create 5 logs at 5-hour intervals, newest first by epoch
    files = [_make_log(home, "T-1", NOW - i * 3600) for i in range(5)]
    count, nbytes = prune_session_logs(_cfg(home, session_log_max_per_ticket=2), "T-1", now=NOW)
    assert count == 3
    assert nbytes > 0
    assert files[0].exists()   # newest
    assert files[1].exists()
    assert not files[2].exists()
    assert not files[3].exists()
    assert not files[4].exists()


def test_prune_max_noop_when_under_limit(home):
    files = [_make_log(home, "T-1", NOW - i * 3600) for i in range(3)]
    count, nbytes = prune_session_logs(_cfg(home, session_log_max_per_ticket=5), "T-1", now=NOW)
    assert (count, nbytes) == (0, 0)
    assert all(f.exists() for f in files)


# ---------------------------------------------------------------------------
# live session is never pruned
# ---------------------------------------------------------------------------

def test_prune_never_removes_live_session_by_age(home):
    old = _make_log(home, "T-1", NOW - 10 * 86400)
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1", log_path=str(old))
    count, nbytes = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1", now=NOW)
    assert (count, nbytes) == (0, 0)
    assert old.exists()


def test_prune_never_removes_live_session_by_max(home):
    files = [_make_log(home, "T-1", NOW - i * 3600) for i in range(5)]
    # Mark the oldest as the live session
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1", log_path=str(files[4]))
    count, nbytes = prune_session_logs(_cfg(home, session_log_max_per_ticket=2), "T-1", now=NOW)
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
    with patch("maestro.claims.pid_alive", return_value=False):
        count, nbytes = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1", now=NOW)
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
        count, nbytes = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    assert count == 1
    assert not old.exists()


# ---------------------------------------------------------------------------
# injectable now= (no more patching store.now_epoch)
# ---------------------------------------------------------------------------

def test_prune_now_kwarg_avoids_patching_store(home):
    old = _make_log(home, "T-1", NOW - 10 * 86400)
    count, _ = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1", now=NOW)
    assert count == 1
    assert not old.exists()


# ---------------------------------------------------------------------------
# unlink(missing_ok=True) no-op must not inflate the counter
# ---------------------------------------------------------------------------

def test_prune_does_not_count_already_missing_file(home):
    old = _make_log(home, "T-1", NOW - 10 * 86400)
    old.unlink()  # gone before prune ever runs
    count, nbytes = prune_session_logs(_cfg(home, session_log_retention_days=5), "T-1", now=NOW)
    assert (count, nbytes) == (0, 0)


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
    assert result["pruned_bytes"] > 0


def test_compact_pruned_logs_zero_when_unconfigured(home):
    from maestro import event_log, snapshot as snap_mod
    event_log.append(home, "T-1", "Note", {"n": 1}, actor="t")
    snap_mod.rebuild(home, "T-1")
    _make_log(home, "T-1", NOW - 10 * 86400)
    result = compact(_cfg(home, session_log_retention_days=0, session_log_max_per_ticket=0), "T-1")
    assert result["pruned_logs"] == 0
