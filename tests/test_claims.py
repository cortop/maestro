import os

from maestro import claims


def test_live_pid_is_claimed(home):
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")  # this process is alive
    assert claims.is_claimed(home, "T-1")
    assert "T-1" in claims.active_keys(home)


def test_dead_pid_is_reclaimed(home):
    dead = 2_000_000_000  # almost certainly not a live pid
    claims.write_claim(home, "T-1", dead, "reconcile-T-1")
    assert not claims.is_claimed(home, "T-1")          # stale -> reclaimable
    assert not claims.claim_path(home, "T-1").exists()  # and cleaned up


def test_release(home):
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")
    claims.release(home, "T-1")
    assert not claims.is_claimed(home, "T-1")
    assert claims.active_keys(home) == set()
