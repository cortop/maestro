"""GA-11: per-day USD spend meter + enforcing daily ceiling.

Every test drives the real surface: a real dispatch(cfg, DryRunSessions(), now=...)
sweep over a temp home, or the real `maestro` CLI -- never a mocked spend module.
Only the `claude -p` spawn (DryRunSessions) is mocked, per CLAUDE.md.
"""
import json

from maestro import dispatcher as disp
from maestro import event_log, snapshot as snap_mod, spend, store
from maestro.cli import main
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _seed(home, key, phase=Phase.READY):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def _write_stream_log(home, key, epoch, records):
    session_id = f"reconcile-{key}-{epoch:.6f}"
    path = home / "agent-logs" / key / f"{session_id}.stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in records)
    path.write_text(text, encoding="utf-8")
    return path


def _result_record(cost):
    return {"type": "result", "total_cost_usd": cost, "num_turns": 3, "duration_ms": 1000,
            "usage": {"input_tokens": 100, "output_tokens": 50}}


def _spawn_and_seed_ledger(home, cfg, key, now):
    """Real first sweep: spawns *key*, populating derived/.spawn_ledger.json --
    the ledger spend.probe reads (mirrors test_ratelimit.py's identical helper)."""
    cfg.min_spawn_interval = 0
    _seed(home, key, Phase.READY)
    report = disp.dispatch(cfg, DryRunSessions(), now=now)
    assert key in report.spawned
    return report


# --- AC2: byte-offset cursor, re-running with no new bytes is a no-op --------

def test_probe_reads_only_new_bytes_and_is_idempotent(home, cfg):
    t0 = 1_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    _write_stream_log(home, "T-1", t0, [_result_record(1.5)])

    st1 = spend.probe(cfg, t0 + 10)
    assert st1["total_usd"] == 1.5

    # Re-running with no new bytes must leave the total unchanged.
    st2 = spend.probe(cfg, t0 + 20)
    assert st2["total_usd"] == 1.5

    cursor = store.read_json(home / "derived" / ".spend_cursor.json", {})
    assert len(cursor) == 1


def test_probe_accumulates_across_multiple_sessions(home, cfg):
    t0 = 1_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    _write_stream_log(home, "T-1", t0, [_result_record(2.0)])
    spend.probe(cfg, t0 + 5)

    t1 = t0 + 600
    _spawn_and_seed_ledger(home, cfg, "T-2", t1)
    _write_stream_log(home, "T-2", t1, [_result_record(3.25)])
    st = spend.probe(cfg, t1 + 5)
    assert st["total_usd"] == 5.25


# --- AC4/AC5: the ceiling gate, proven to fire at the boundary ---------------

def test_dispatch_blocks_spawns_at_or_above_ceiling(home, cfg):
    t0 = 1_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    _write_stream_log(home, "T-1", t0, [_result_record(10.00)])
    spend.probe(cfg, t0 + 5)

    cfg.daily_spend_ceiling_usd = 10.00
    _seed(home, "T-2", Phase.READY)
    ledger_before = store.read_json(home / "derived" / ".spawn_ledger.json", {})

    report = disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)

    assert report.spawned == []
    assert report.spend_ceiling_reason is not None
    ledger_after = store.read_json(home / "derived" / ".spawn_ledger.json", {})
    assert ledger_after == ledger_before


def test_dispatch_spawns_normally_one_cent_below_ceiling(home, cfg):
    t0 = 1_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    _write_stream_log(home, "T-1", t0, [_result_record(9.99)])
    spend.probe(cfg, t0 + 5)

    cfg.daily_spend_ceiling_usd = 10.00
    cfg.min_spawn_interval = 0
    _seed(home, "T-2", Phase.READY)

    report = disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)

    assert "T-2" in report.spawned
    assert report.spend_ceiling_reason is None


def test_dispatch_still_spawns_with_no_ceiling_configured(home, cfg):
    """RB-8: an unset daily_spend_ceiling_usd (the default) must keep failing
    OPEN -- visibility (health.check_daily_spend's warn status) is this
    ticket's fix, blocking is explicitly NOT. A real dispatch() sweep, real
    spend already folded well above any sane ceiling, still spawns."""
    t0 = 1_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    _write_stream_log(home, "T-1", t0, [_result_record(845.00)])  # the 2026-07-19 figure
    spend.probe(cfg, t0 + 5)

    assert cfg.daily_spend_ceiling_usd is None
    cfg.min_spawn_interval = 0
    _seed(home, "T-2", Phase.READY)

    report = disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)

    assert "T-2" in report.spawned
    assert report.spend_ceiling_reason is None


# --- AC6: text-format homes report spend as unavailable, never $0.00 --------

def test_text_format_home_reports_spend_unavailable_not_zero(home, cfg):
    cfg.session_log_format = "text"
    st = spend.status(cfg, store.now_epoch())
    assert st["unavailable"] is True
    assert st["today_usd"] is None

    cfg.daily_spend_ceiling_usd = 0.01  # would block any dollar figure at all
    _seed(home, "T-1", Phase.READY)
    cfg.min_spawn_interval = 0
    report = disp.dispatch(cfg, DryRunSessions(), now=store.now_epoch())
    assert "T-1" in report.spawned
    assert report.spend_ceiling_reason is None


def test_doctor_reports_unavailable_for_text_format(home):
    store.atomic_write(home / "config.toml", '[maestro]\nsession_log_format = "text"\n')
    import io
    import sys

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = main(["--home", str(home), "doctor"])
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    assert rc == 0
    assert out["spend_unavailable"] is True
    assert out["spend_today_usd"] is None


# --- AC8: a corrupt/garbage spend state does not abort the sweep -----------

def test_corrupt_spend_state_does_not_abort_sweep(home, cfg):
    t0 = 1_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    # Valid JSON, wrong shape (a bare string, not an object) -- garbage the
    # meter cannot make sense of.
    store.atomic_write(home / "derived" / ".spend.json", json.dumps("not-an-object"))

    _seed(home, "T-2", Phase.READY)
    cfg.min_spawn_interval = 0
    report = disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)

    assert "T-2" in report.spawned
    assert "spend_probe" in report.hook_errors


def test_status_never_raises_on_corrupt_state(home, cfg):
    store.atomic_write(home / "derived" / ".spend.json", json.dumps(["garbage", "list"]))
    st = spend.status(cfg, store.now_epoch())
    assert st["today_usd"] == 0.0
    assert st["unavailable"] is False


# --- doctor / TUI surfacing --------------------------------------------------

def test_doctor_json_includes_spend_fields(home, cfg):
    # `maestro doctor` reads spend history against the REAL wall clock
    # (cmd_doctor's `store.now_epoch()`), so this sweep must be timestamped
    # near real "now" -- a synthetic epoch would fold into a different UTC
    # date bucket than the one doctor reads back (see test_health.py's
    # identical incident-replay comment).
    t0 = store.now_epoch()
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    _write_stream_log(home, "T-1", t0, [_result_record(4.5)])
    spend.probe(cfg, t0 + 5)
    store.atomic_write(home / "config.toml", "[maestro]\ndaily_spend_ceiling_usd = 25.0\n")

    import io
    import sys

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = main(["--home", str(home), "doctor"])
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    assert rc == 0
    assert out["spend_today_usd"] == 4.5
    assert out["spend_ceiling_usd"] == 25.0
    names = {c["name"] for c in out["checks"]}
    assert "daily_spend" in names
