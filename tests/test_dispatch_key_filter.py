"""MTO-4: `maestro dispatch --key <KEY>` restricts a sweep's candidate set to the
named ticket(s), resolved before due-checking, so everything downstream (due-check,
throttle, claims, spawn ledger, reconcile-command routing) behaves normally but only
ever considers them -- composes with --dry-run/--model, idles instead of substituting
a throttled target, never mints, and rejects an unknown key. Every test drives the
real surface: a real `dispatch(cfg, sessions, ..., key_filter=...)` (or the real CLI
`cli.main([...])`) over a temp home; the only substituted boundary is DryRunSessions
(no real `claude -p` launch)."""
import io
import json
import sys

import pytest

from maestro import cli, dispatcher as disp, event_log, inbox, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _seed(home, key, phase=Phase.READY):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated",
                      {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def _run_cli(home, *args):
    buf_out, buf_err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf_out, buf_err
    try:
        code = cli.main(["--home", str(home), *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, buf_out.getvalue(), buf_err.getvalue()


def test_key_filter_spawns_only_the_named_ticket(home, cfg):
    """AC1: a real sweep restricted to T-1 spawns T-1, leaves T-2 untouched (not
    due-checked into `due`, not spawned, no spawn-ledger entry)."""
    _seed(home, "T-1", Phase.READY)
    _seed(home, "T-2", Phase.READY)
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000, key_filter=["T-1"])
    assert report.spawned == ["T-1"]
    assert report.due == [("T-1", "active")]  # T-2 never even entered due-checking
    ledger = store.read_json(disp._spawn_ledger_path(home), {})
    assert set(ledger) == {"T-1"}


def test_key_filter_composes_with_dry_run(home, cfg):
    """AC2: --key + --dry-run reports would_spawn for that key alone and writes
    nothing (no spawn-ledger entry, no new events for either ticket)."""
    _seed(home, "T-1", Phase.READY)
    _seed(home, "T-2", Phase.READY)
    seq_before = {k: event_log.last_seq(home, k) for k in ("T-1", "T-2")}
    report = disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True, key_filter=["T-1"])
    assert report.would_mint == []
    assert report.spawned == ["T-1"]  # DispatchReport.spawned doubles as would_spawn under dry_run
    assert not disp._spawn_ledger_path(home).exists()
    assert {k: event_log.last_seq(home, k) for k in ("T-1", "T-2")} == seq_before


def test_key_filter_throttled_target_idles_instead_of_substituting(home, cfg):
    """AC3: today's substitution (spawn a different due key when the requested one
    is floored) must NOT happen inside a --key-restricted sweep -- it idles."""
    _seed(home, "T-1", Phase.READY)
    _seed(home, "T-2", Phase.READY)  # also due, would normally be substituted in
    cfg.min_spawn_interval = 300
    store.write_json(disp._spawn_ledger_path(home), {"T-1": 999.0})
    report = disp.dispatch(cfg, DryRunSessions(), now=1000, key_filter=["T-1"])
    assert report.spawned == []
    assert report.throttled == ["T-1"]


def test_unrestricted_sweep_still_substitutes_on_throttle(home, cfg):
    """AC4: outside a --key sweep, the existing slot-substitution behaviour --
    exactly the bug MTO-4 exists to make optional-out-of -- is unchanged."""
    _seed(home, "T-1", Phase.READY)
    _seed(home, "T-2", Phase.READY)
    cfg.min_spawn_interval = 300
    store.write_json(disp._spawn_ledger_path(home), {"T-1": 999.0})
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.throttled == ["T-1"]
    assert report.spawned == ["T-2"]  # substituted in place of the floored T-1


def test_key_filter_repeat_and_comma_separated(home):
    """AC5 (repeatable/comma-separated): --key T-1 --key T-2,T-3 restricts to all three."""
    _seed(home, "T-1", Phase.READY)
    _seed(home, "T-2", Phase.READY)
    _seed(home, "T-3", Phase.READY)
    (home / "config.toml").write_text("[maestro]\nmax_concurrency = 3\n")
    code, out, _ = _run_cli(home, "dispatch", "--dry-run", "--key", "T-1", "--key", "T-2,T-3")
    assert code == 0
    report = json.loads(out)
    assert sorted(report["would_spawn"]) == ["T-1", "T-2", "T-3"]


def test_key_filter_unknown_key_is_a_clear_error(home, cfg):
    """AC5 (unknown key): a clear error, never a silent empty sweep."""
    _seed(home, "T-1", Phase.READY)
    with pytest.raises(store.MaestroError, match="unknown ticket key"):
        disp.dispatch(cfg, DryRunSessions(), now=1000, key_filter=["NOPE-1"])


def test_key_filter_unknown_key_cli_error():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        for d in ("events", "inbox", "tickets", "worktrees", "derived/snapshots", "derived/cursors"):
            (home / d).mkdir(parents=True, exist_ok=True)
        code, out, err = _run_cli(home, "dispatch", "--key", "NOPE-1")
        assert code == 2
        assert out == ""
        assert "unknown ticket key" in err


def test_key_filter_resolves_reconcile_command_identically(home, cfg):
    """AC6: a --key sweep routes the spawn through resolve_reconcile_command exactly
    like an unrestricted sweep -- same call site, not re-derived."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000, key_filter=["T-1"])
    assert len(sessions.spawned) == 1
    prompt = sessions.spawned[0][1]
    expected = disp.resolve_reconcile_command(cfg, Phase.IMPLEMENTING.value)
    assert prompt == f"{expected} T-1"


def test_key_filter_never_mints(home, cfg):
    """A --key sweep never mints, not even a request that happens to name the
    filtered key -- the _new inbox is keyless and left for the next unrestricted
    sweep instead of being partially/silently drained."""
    _seed(home, "T-1", Phase.READY)
    inbox.append_new(home, "brand new", key="T-2")
    report = disp.dispatch(cfg, DryRunSessions(), now=1000, key_filter=["T-1"])
    assert report.minted == []
    assert "T-2" not in disp.list_keys(home)
    # left pending for a real, unrestricted sweep to mint
    assert len(inbox.pending_new(home)) == 1
