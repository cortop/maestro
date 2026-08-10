import json
import os
import subprocess
import sys

import pytest

from maestro import claims, event_log, store
from maestro.cli import main as cli_main


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


def test_write_claim_persists_cwd_and_prompt(home):
    """T-45: a later sweep, finding this claim's process already dead, needs
    the resolved reconcile command + session cwd to name in a Failed event --
    both must survive on the claim itself, not just in the caller's locals."""
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1",
                       cwd="/some/worktree/T-1",
                       prompt="/maestro-reconcile-implementing T-1")
    c = claims.read_claim(home, "T-1")
    assert c["cwd"] == "/some/worktree/T-1"
    assert c["prompt"] == "/maestro-reconcile-implementing T-1"


def test_write_claim_omits_cwd_and_prompt_when_not_given(home):
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")
    c = claims.read_claim(home, "T-1")
    assert "cwd" not in c
    assert "prompt" not in c


# ---------------------------------------------------------------------------
# Verified identity (T-17): a three-valued verdict, matching process start time
# (from real `ps`) against the claim's recorded epoch — pid reuse means the live
# process started AFTER the claim, so it's "denied", never "confirmed".
# ---------------------------------------------------------------------------

@pytest.fixture
def child_process():
    """A real, live, non-reconciler process — the pid-reuse hazard's stand-in."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_confirmed_when_epoch_matches_true_start(home, child_process):
    claims.write_claim(home, "T-1", child_process.pid, "reconcile-T-1")
    assert claims.verify_claim(home, "T-1") == "confirmed"
    assert claims.is_claimed(home, "T-1")
    assert claims.claim_path(home, "T-1").exists()


def test_denied_when_pid_was_reused(home, child_process):
    """A claim whose recorded epoch predates the live process's true start time
    can only mean pid reuse: the real reconciler is gone, this is someone else's
    process — denied, and released immediately (no kill)."""
    old_epoch = store.now_epoch() - 3600
    store.write_json(claims.claim_path(home, "T-1"),
                     {"pid": child_process.pid, "name": "reconcile-T-1",
                      "ts": store.iso_now(), "epoch": old_epoch})
    assert claims.verify_claim(home, "T-1") == "denied"
    assert not claims.is_claimed(home, "T-1")
    assert not claims.claim_path(home, "T-1").exists()


def test_malformed_and_out_of_range_pids_dont_poison_the_batch(home, child_process):
    """`ps -p` aborts its ENTIRE query on one out-of-range pid — sanitize before
    batching so a garbage claim can't blind the probe to every other claim."""
    claims.write_claim(home, "T-live", child_process.pid, "reconcile-T-live")
    store.write_json(claims.claim_path(home, "T-huge"),
                     {"pid": 2_000_000_000, "name": "reconcile-T-huge", "epoch": store.now_epoch()})
    store.write_json(claims.claim_path(home, "T-neg"),
                     {"pid": -1, "name": "reconcile-T-neg", "epoch": store.now_epoch()})
    store.write_json(claims.claim_path(home, "T-str"),
                     {"pid": "abc", "name": "reconcile-T-str", "epoch": store.now_epoch()})

    assert claims.active_keys(home) == {"T-live"}
    assert claims.claim_path(home, "T-live").exists()
    assert not claims.claim_path(home, "T-huge").exists()
    assert not claims.claim_path(home, "T-neg").exists()
    assert not claims.claim_path(home, "T-str").exists()


class _FailingProc:
    returncode = 1
    stdout = ""
    stderr = "ps: no such option"


def _failing_run(cmd, **kw):
    return _FailingProc()


def test_unverified_claim_honored_until_ceiling_then_released(home):
    """`unknown` (here: the probe itself fails) falls back to raw pid liveness,
    capped by `max_age` — no FAILED event either way, that stays T-13's job."""
    now = store.now_epoch()
    claims.write_claim(home, "T-young", os.getpid(), "reconcile-T-young")
    store.write_json(claims.claim_path(home, "T-old"),
                     {"pid": os.getpid(), "name": "reconcile-T-old", "epoch": now - 100_000})

    active = claims.active_keys(home, run=_failing_run, max_age=3600)

    assert active == {"T-young"}
    assert claims.claim_path(home, "T-young").exists()
    assert not claims.claim_path(home, "T-old").exists()
    for key in ("T-young", "T-old"):
        assert event_log.read(home, key) == []


def test_probe_forks_at_most_once_for_active_keys(home):
    calls = []

    def counting_run(cmd, **kw):
        calls.append(cmd)
        return _FailingProc()

    for i in range(8):
        store.write_json(claims.claim_path(home, f"T-{i}"),
                         {"pid": 10_000 + i, "name": f"reconcile-T-{i}", "epoch": store.now_epoch()})

    claims.active_keys(home, run=counting_run)
    assert len(calls) == 1


def test_describe_claims_rows(home, child_process):
    claims.write_claim(home, "T-1", child_process.pid, "reconcile-T-1")
    rows = claims.describe_claims(home)
    assert len(rows) == 1
    row = rows[0]
    assert row["key"] == "T-1"
    assert row["pid"] == child_process.pid
    assert row["verdict"] == "confirmed"
    assert row["claimed"] is True
    assert isinstance(row["age_s"], int)


# ---------------------------------------------------------------------------
# `maestro claims [--purge]`
# ---------------------------------------------------------------------------

def test_cli_claims_lists_key_pid_age_verdict(home, capsys, child_process):
    claims.write_claim(home, "T-1", child_process.pid, "reconcile-T-1")
    rc = cli_main(["--home", str(home), "claims"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    row = rows[0]
    assert row["key"] == "T-1"
    assert row["pid"] == child_process.pid
    assert row["verdict"] == "confirmed"
    assert isinstance(row["age_s"], int)


def test_cli_claims_purge_drops_denied_and_over_age_only(home, capsys, child_process):
    now = store.now_epoch()
    # confirmed: survives
    claims.write_claim(home, "T-confirmed", child_process.pid, "reconcile-T-confirmed")
    # denied: pid reused (epoch predates the live child)
    store.write_json(claims.claim_path(home, "T-denied"),
                     {"pid": child_process.pid, "name": "reconcile-T-denied",
                      "ts": store.iso_now(), "epoch": now - 3600})
    # unknown + over-age: probe can't resolve a garbage pid, and it's ancient
    store.write_json(claims.claim_path(home, "T-stale"),
                     {"pid": 2_000_000_000, "name": "reconcile-T-stale", "epoch": now - 100_000_000})

    rc = cli_main(["--home", str(home), "claims", "--purge"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["purged"]) == {"T-denied", "T-stale"}
    assert claims.claim_path(home, "T-confirmed").exists()
    assert not claims.claim_path(home, "T-denied").exists()
    assert not claims.claim_path(home, "T-stale").exists()


def test_cli_release_unchanged(home, capsys):
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")
    rc = cli_main(["--home", str(home), "release", "T-1"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"released": "T-1"}
    assert not claims.claim_path(home, "T-1").exists()


# ---------------------------------------------------------------------------
# unverified_claim_max_age config knob
# ---------------------------------------------------------------------------

def test_unverified_claim_max_age_in_default_config_toml():
    from maestro.config import DEFAULT_CONFIG_TOML
    assert "unverified_claim_max_age" in DEFAULT_CONFIG_TOML


def test_unverified_claim_max_age_parses_from_config_toml(home):
    from maestro.config import load

    (home / "config.toml").write_text(
        "[maestro]\nunverified_claim_max_age = 7200\n", encoding="utf-8")
    cfg = load(str(home))
    assert cfg.unverified_claim_max_age == 7200


def test_unverified_claim_max_age_defaults_to_24h(home):
    from maestro.config import load
    cfg = load(str(home))
    assert cfg.unverified_claim_max_age == 24 * 3600
