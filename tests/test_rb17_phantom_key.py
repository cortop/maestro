"""RB-17: a reconciler must never be spawnable for a key with no spec.md -- and,
in failing to find one, must never mint the phantom ticket its own failure would
otherwise create. Every test drives the real surface: `dispatcher.dispatch()`,
`ops` verbs, `health.check_phantom_keys`, and the real CLI over a temp home --
never a mock of the guard itself."""
import io
import sys

import pytest

from maestro import cli, dispatcher as disp, event_log, health, inbox, ops
from maestro import snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _seed(home, key, phase=Phase.READY):
    """A real, fully-minted ticket -- spec.md + TicketCreated + PhaseChanged."""
    store.atomic_write(store.spec_path(home, key), f"# {key}\n")
    event_log.append(home, key, "TicketCreated",
                      {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def _seed_legacy_phantom(home, key, phase=Phase.DEGRADED):
    """The T-999 shape AFTER it already bootstrapped -- a real event log
    (appended directly via `event_log.append`, bypassing `ops`, exactly as a
    reconciler's raw `Failed` append would have before this guard existed),
    but no spec.md and no ticket directory. Simulates a phantom that predates
    RB-17's guards -- what AC4 requires stays disposable."""
    event_log.append(home, key, "Failed",
                      {"error": "no spec.md or ticket directory found"}, actor="reconciler")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="reconciler")
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


# --- AC1/AC2: a never-minted key is never spawned, and closes its own bootstrap ---

def test_never_minted_key_never_spawns_and_appends_nothing(home, cfg):
    """AC1: a real dispatch() sweep over a temp home containing a key with a
    stray derived snapshot (the exact self-bootstrap artifact this ticket
    closes -- see module docstring) but no ticket directory and no spec.md
    records zero spawns and appends no event."""
    snap_mod.rebuild(home, "T-999")  # the phantom-fold artifact, no log/spec/dir behind it
    assert store.snapshot_path(home, "T-999").exists()
    assert "T-999" in disp.list_keys(home)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.spawned == []
    assert event_log.read(home, "T-999") == []


def test_phantom_snapshot_is_pruned_so_it_stops_being_permanently_swept(home, cfg):
    """AC2: after that sweep the key has no event log and no snapshot -- it
    does not become permanently sweepable via `list_keys`'s snapshot scan."""
    snap_mod.rebuild(home, "T-999")
    disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert not store.snapshot_path(home, "T-999").exists()
    assert not store.events_path(home, "T-999").exists()
    assert "T-999" not in disp.list_keys(home)


def test_never_minted_key_decision_is_visible_via_why(home, cfg):
    """The skip is surfaced via the decision ledger (`maestro why`), never
    silently ignored."""
    snap_mod.rebuild(home, "T-999")
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    decisions = disp.key_decisions(home, "T-999")
    assert decisions and decisions[-1]["outcome"] == "phantom"


def test_dry_run_sweep_never_deletes_the_stray_snapshot(home, cfg):
    """`dry_run` (GA-4) is strictly read-only -- it must not prune the
    phantom snapshot artifact either, only skip spawning it."""
    snap_mod.rebuild(home, "T-999")
    report = disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)
    assert report.spawned == []
    assert store.snapshot_path(home, "T-999").exists()


# --- AC3: dispatch attempted for a nonexistent key is a clear, attributable error ---

def test_dispatch_key_filter_refuses_a_never_minted_key_even_with_a_stray_snapshot(home, cfg):
    """AC3: `dispatch --key` already refuses an outright-unknown key; this
    proves the same clear error now also covers a key that IS a `list_keys`
    member (a stray snapshot) but was never actually minted -- the exact
    T-999 shape, produced by a real `dispatch(key_filter=...)` call."""
    snap_mod.rebuild(home, "T-999")
    with pytest.raises(store.MaestroError, match="unknown ticket key"):
        disp.dispatch(cfg, DryRunSessions(), now=1000, key_filter=["T-999"])
    # No event/snapshot survives the refused attempt.
    assert event_log.read(home, "T-999") == []


def test_reconciler_verbs_refuse_to_bootstrap_a_never_minted_key(home, cfg):
    """AC3 (the compensating control, entry-point-agnostic): a reconciler
    mistakenly spawned for a key with no footprint at all -- however it got
    spawned -- gets a clear, attributable `MaestroError` the moment it tries
    to record ANYTHING (`maestro fail`, the exact call the old T-999 incident
    made four times), instead of that call silently manufacturing the
    phantom's first event. Retried the incident's own way (repeated calls)
    to prove it never grows a dead-letter/Failed pile -- the after-state is
    identical to the before-state every time."""
    for _ in range(4):
        with pytest.raises(store.MaestroError, match="never minted"):
            ops.fail(cfg, "T-999", "no spec.md or ticket directory found under tickets/T-999")
    assert event_log.read(home, "T-999") == []
    assert not store.deadletter_path(home, "T-999").exists()
    assert not store.snapshot_path(home, "T-999").exists()


def test_reconciler_ask_and_requeue_also_refuse_a_never_minted_key(home, cfg):
    """The same guard covers every other mutating verb named in the spec
    (`ask`/`requeue`), not just `fail` -- all funnel through the one `_append`
    chokepoint."""
    with pytest.raises(store.MaestroError, match="never minted"):
        ops.ask(cfg, "T-999", "does this ticket exist?")
    with pytest.raises(store.MaestroError, match="never minted"):
        ops.requeue(cfg, "T-999", 60)
    with pytest.raises(store.MaestroError, match="never minted"):
        ops.set_phase(cfg, "T-999", Phase.TERMINATING, reason="human: discard")
    assert event_log.read(home, "T-999") == []


def test_observe_spec_still_no_ops_gracefully_for_a_missing_spec(home, cfg):
    """`maestro observe-spec` -- the very first call every reconcile skill's
    preamble makes -- must keep degrading to a quiet no-op for a missing
    spec.md rather than raising; the guard belongs on the verbs that would
    actually mint the phantom, not on this read-then-maybe-note helper."""
    assert ops.observe_spec(cfg, "T-999") is None
    assert event_log.read(home, "T-999") == []


# --- AC4: an EXISTING phantom with real event-log history stays fully disposable ---

def test_existing_phantom_with_real_log_is_still_swept_and_disposable(home, cfg):
    """AC4: a phantom that already has event-log history (however it got
    there) is NOT `_never_minted` -- the dispatcher keeps sweeping it
    normally, and `maestro cmd <KEY> discard` retires it all the way to
    `done`, proven end-to-end against the real CLI + real `ops` verbs (the
    same calls the `awaiting-human`/`passive` reconcile skills make)."""
    _seed_legacy_phantom(home, "T-999", phase=Phase.DEGRADED)
    assert not store.spec_path(home, "T-999").exists()

    code, _, _ = _run_cli(home, "cmd", "T-999", "discard", "--no-nudge")
    assert code == 0
    assert inbox.has_pending(home, "T-999")

    # A real sweep must still consider this key -- it has real history, so
    # `_never_minted` is False and the inbox command makes it due.
    snap = snap_mod.load(home, "T-999")
    res = disp.is_due(home, "T-999", snap, inbox_pending=True,
                       current_spec_hash=disp.spec_hash_on_disk(home, "T-999"), now=1000)
    assert res.due and res.reason == "inbox"

    # What the `passive` reconcile skill does on `discard`: fold the inbox,
    # then route to `terminating`, then finalize -- all real `ops` calls,
    # none blocked by the never-minted guard since this key already has a log.
    ops.fold_inbox(cfg, "T-999")
    ops.set_phase(cfg, "T-999", Phase.TERMINATING, reason="human: discard")
    ops.finalize(cfg, "T-999")

    assert snap_mod.load(home, "T-999").phase == Phase.DONE.value


def test_never_minted_guard_does_not_shadow_a_key_with_only_a_ticket_dir(home, cfg):
    """A ticket directory alone (spec.md deleted after minting, log intact)
    also keeps the key out of `_never_minted` -- only the fully-empty shape
    (no dir, no spec, no log) is refused."""
    store.ticket_dir(home, "T-999").mkdir(parents=True)
    event_log.append(home, "T-999", "TicketCreated", {"title": "T-999"}, actor="d")
    assert disp._never_minted(home, "T-999") is False


# --- part (c): `maestro doctor` flags an existing phantom for manual triage ---

def test_doctor_flags_an_existing_phantom_key_but_not_a_real_ticket(home, cfg):
    _seed(home, "T-1", Phase.READY)
    _seed_legacy_phantom(home, "T-999")
    result = health.check_phantom_keys(cfg, 1000)
    assert result["status"] == "warn"
    assert result["keys"] == ["T-999"]
    assert "T-1" not in result["keys"]


def test_doctor_ok_when_no_phantoms(home, cfg):
    _seed(home, "T-1", Phase.READY)
    result = health.check_phantom_keys(cfg, 1000)
    assert result["status"] == "ok"
    assert result["keys"] == []


def test_doctor_registers_phantom_keys_check(home, cfg):
    assert health.check_phantom_keys in health.CHECKS
    names = [c["name"] for c in health.run_checks(cfg, 1000)]
    assert names.count("phantom_keys") == 1


# --- AC5: legitimate ticket-creating flows are unaffected -------------------

def test_minting_from_new_inbox_is_unaffected(home, cfg):
    """Minting writes spec.md before its first event (`_mint_ticket`, called
    directly with `event_log.append`, never through the guarded `ops._append`
    chokepoint) -- unaffected by either of RB-17's new guards, proven over a
    real sweep."""
    inbox.append_new(home, "a brand new ticket", key=None)
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert len(report.minted) == 1
    key = report.minted[0]
    assert store.spec_path(home, key).exists()
    assert event_log.read(home, key)
    assert disp._never_minted(home, key) is False
