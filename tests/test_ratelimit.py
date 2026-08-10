"""Fleet-wide rate-limit gate: a rejected rate_limit_event pauses every spawn.

Every test drives the real surface: a real dispatch(cfg, DryRunSessions(), now=...)
sweep over a temp home, or the real `maestro` CLI — never a mocked ratelimit module.
"""
import json

import pytest

from maestro import dispatcher as disp
from maestro import event_log, ratelimit, snapshot as snap_mod, store
from maestro.cli import main
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _seed(home, key, phase=Phase.READY):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def _rate_limit_event(status, resets_at, rate_limit_type="five_hour", **extra):
    info = {"status": status, "resetsAt": resets_at, "rateLimitType": rate_limit_type, **extra}
    return {"type": "rate_limit_event", "rate_limit_info": info,
            "uuid": "u-1", "session_id": "sess-1"}


def _write_stream_log(home, key, epoch, records, *, fmt="stream.jsonl"):
    session_id = f"reconcile-{key}-{epoch:.6f}"
    path = home / "agent-logs" / key / f"{session_id}.{fmt}"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in records)
    path.write_text(text, encoding="utf-8")
    return path


def _append_records(path, records):
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _spawn_and_seed_ledger(home, cfg, key, now):
    """Real first sweep: mints nothing (ticket pre-seeded READY), spawns *key*,
    and writes derived/.spawn_ledger.json — the ledger ratelimit.probe reads.

    Disables the (unrelated) spawn-rate floor so repeat sweeps in these tests
    are governed only by the rate-limit gate under test.
    """
    cfg.min_spawn_interval = 0
    _seed(home, key, Phase.READY)
    report = disp.dispatch(cfg, DryRunSessions(), now=now)
    assert key in report.spawned
    assert store.read_json(home / "derived" / ".spawn_ledger.json", {}).get(key, {}).get("last") == now
    return report


# ---------------------------------------------------------------------------
# AC1: the probe fires only on a real rejection
# ---------------------------------------------------------------------------

def test_probe_pauses_on_real_rejection(home, cfg):
    cfg.ratelimit_grace = 60
    t0 = 1_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)

    resets_at = t0 + 500
    _write_stream_log(home, "T-1", t0, [_rate_limit_event("rejected", resets_at)])

    t1 = t0 + 10
    report = disp.dispatch(cfg, DryRunSessions(), now=t1)

    state = store.read_json(home / "derived" / ".ratelimit.json", None)
    assert state is not None
    assert state["paused_until"] == resets_at + cfg.ratelimit_grace
    assert state["rate_limit_type"] == "five_hour"
    assert report.paused_until == state["paused_until"]


@pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
def test_probe_ignores_non_rejected_status(home, cfg, status):
    t0 = 2_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    _write_stream_log(home, "T-1", t0, [_rate_limit_event(status, t0 + 500)])

    t1 = t0 + 10
    disp.dispatch(cfg, DryRunSessions(), now=t1)
    assert not (home / "derived" / ".ratelimit.json").exists()

    # Still spawns on the next sweep (nothing paused).
    _seed(home, "T-1", Phase.READY)  # re-affirm READY (no-op if already there)
    report = disp.dispatch(cfg, DryRunSessions(), now=t1 + 400)
    assert "T-1" in report.spawned


# ---------------------------------------------------------------------------
# AC2: the gate is absolute and ledger-neutral
# ---------------------------------------------------------------------------

def test_gate_blocks_all_spawns_including_unthrottled_human_reason(home, cfg):
    t0 = 3_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    resets_at = t0 + 500
    _write_stream_log(home, "T-1", t0, [_rate_limit_event("rejected", resets_at)])
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)  # detect + pause

    ledger_before = (home / "derived" / ".spawn_ledger.json").read_bytes()

    # A second ticket becomes due via the unthrottled "inbox" human-signal reason.
    from maestro import inbox
    _seed(home, "T-2", Phase.AWAITING_HUMAN)
    event_log.append(home, "T-2", "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r")
    snap_mod.rebuild(home, "T-2")
    inbox.append_command(home, "T-2", "ans", {"qid": "q1", "text": "go"})

    report = disp.dispatch(cfg, DryRunSessions(), now=t0 + 20)
    assert report.spawned == []
    assert report.paused_until is not None
    ledger_after = (home / "derived" / ".spawn_ledger.json").read_bytes()
    assert ledger_after == ledger_before

    # A create-request still mints during the same paused sweep.
    from maestro import inbox as inbox_mod
    inbox_mod.append_new(home, "queued while paused", key="T-9")
    report2 = disp.dispatch(cfg, DryRunSessions(), now=t0 + 21)
    assert "T-9" in report2.minted
    assert report2.spawned == []


def test_pause_expires_and_state_removed(home, cfg):
    t0 = 4_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    resets_at = t0 + 100
    _write_stream_log(home, "T-1", t0, [_rate_limit_event("rejected", resets_at)])
    report = disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)
    paused_until = report.paused_until
    assert paused_until is not None

    # Still paused just before the deadline.
    _seed(home, "T-1", Phase.READY)
    report_mid = disp.dispatch(cfg, DryRunSessions(), now=paused_until - 1)
    assert report_mid.spawned == []
    assert (home / "derived" / ".ratelimit.json").exists()

    # Past the deadline: spawns again and the state file is gone.
    report_after = disp.dispatch(cfg, DryRunSessions(), now=paused_until + 1)
    assert "T-1" in report_after.spawned
    assert report_after.paused_until is None
    assert not (home / "derived" / ".ratelimit.json").exists()


# ---------------------------------------------------------------------------
# AC3: degenerate resetsAt values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("resets_at_raw", [None, "not-a-number", 500])
def test_degenerate_resets_at_falls_back(home, cfg, resets_at_raw):
    cfg.ratelimit_fallback_pause = 900
    now = 1_000_000  # resets_at_raw=500 is safely in the past relative to `now`
    record = _rate_limit_event("rejected", resets_at_raw)
    pause_target, resets_at = ratelimit._compute_paused_until(resets_at_raw, now, cfg)
    assert pause_target == now + cfg.ratelimit_fallback_pause


def test_resets_at_beyond_cap_is_clamped(home, cfg):
    cfg.ratelimit_max_pause = 3600
    cfg.ratelimit_grace = 60
    now = 1_000_000
    seven_day_resets_at = now + 7 * 86400
    pause_target, _ = ratelimit._compute_paused_until(seven_day_resets_at, now, cfg)
    assert pause_target == now + cfg.ratelimit_max_pause


def test_millisecond_scale_value_normalized_to_seconds(home, cfg):
    now = 1_780_000_000  # realistic epoch seconds, so the ms-scale twin exceeds 1e12
    resets_at_s = now + 500
    resets_at_ms = resets_at_s * 1000
    pause_target, normalized = ratelimit._compute_paused_until(resets_at_ms, now, cfg)
    assert normalized == resets_at_s
    assert pause_target == resets_at_s + cfg.ratelimit_grace


def test_ratelimit_max_pause_zero_disables_gate_via_real_config(home):
    """A real config.toml with ratelimit_max_pause = 0 loaded through Config.load
    disables the gate end-to-end: a rejected log still spawns."""
    (home / "config.toml").write_text(
        "[maestro]\nmax_concurrency = 3\nratelimit_max_pause = 0\n")
    from maestro.config import load
    cfg = load(str(home))

    t0 = 5_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    _write_stream_log(home, "T-1", t0, [_rate_limit_event("rejected", t0 + 500)])

    _seed(home, "T-1", Phase.READY)
    report = disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)
    assert not (home / "derived" / ".ratelimit.json").exists()
    assert "T-1" in report.spawned
    assert report.paused_until is None


# ---------------------------------------------------------------------------
# AC4: incremental, bounded reading
# ---------------------------------------------------------------------------

def test_cursor_holds_offset_equal_to_log_size(home, cfg):
    t0 = 6_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    path = _write_stream_log(home, "T-1", t0, [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        _rate_limit_event("allowed", t0 + 500),
    ])
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 5)
    cursor = store.read_json(home / "derived" / ".ratelimit_cursor.json", {})
    assert cursor.get(str(path)) == path.stat().st_size


def test_rejection_appended_at_nonzero_offset_is_detected_next_sweep(home, cfg):
    t0 = 7_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    path = _write_stream_log(home, "T-1", t0, [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        _rate_limit_event("allowed", t0 + 500),
    ])
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 1)
    assert not (home / "derived" / ".ratelimit.json").exists()
    offset_before = store.read_json(home / "derived" / ".ratelimit_cursor.json", {})[str(path)]
    assert offset_before > 0

    resets_at = t0 + 600
    _append_records(path, [_rate_limit_event("rejected", resets_at)])
    report = disp.dispatch(cfg, DryRunSessions(), now=t0 + 2)
    state = store.read_json(home / "derived" / ".ratelimit.json", None)
    assert state is not None
    assert state["paused_until"] == resets_at + cfg.ratelimit_grace
    assert report.paused_until is not None


def test_key_absent_from_ledger_is_never_read(home, cfg):
    """A stream log for a key that was never spawned (not in the ledger) is
    ignored even though its newest log carries a rejection."""
    _seed(home, "T-1", Phase.AWAITING_HUMAN)  # no ledger entry: never spawned
    event_log.append(home, "T-1", "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r")
    snap_mod.rebuild(home, "T-1")
    _write_stream_log(home, "T-1", 8_000_000.0, [_rate_limit_event("rejected", 8_000_500)])

    disp.dispatch(cfg, DryRunSessions(), now=8_000_010)
    assert not (home / "derived" / ".ratelimit.json").exists()


def test_text_format_newest_log_is_skipped_without_raising(home, cfg):
    t0 = 9_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    # A .log (text format) session is newer than nothing else -- newest and only.
    path = home / "agent-logs" / "T-1" / f"reconcile-T-1-{t0:.6f}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("plain text transcript, not stream-json\n", encoding="utf-8")

    report = disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)  # must not raise
    assert report.paused_until is None
    assert not (home / "derived" / ".ratelimit.json").exists()


# ---------------------------------------------------------------------------
# T-47: an older un-drained stream-json log must not be shadowed by a newer
# log of a different format -- probe drains every stream-json candidate, not
# just candidates[0].
# ---------------------------------------------------------------------------

def test_older_undrained_log_trips_pause_despite_newer_nonstream_log(home, cfg):
    cfg.ratelimit_grace = 60
    t0 = 10_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)

    resets_at = t0 + 500
    older_path = _write_stream_log(home, "T-1", t0, [_rate_limit_event("rejected", resets_at)])
    # Newer log for the same key, but text-format -- under the old
    # candidates[0]-only logic this would shadow the older log forever.
    newer_path = home / "agent-logs" / "T-1" / f"reconcile-T-1-{t0 + 1:.6f}.log"
    newer_path.write_text("plain text transcript, not stream-json\n", encoding="utf-8")

    t1 = t0 + 10
    report = disp.dispatch(cfg, DryRunSessions(), now=t1)

    state = store.read_json(home / "derived" / ".ratelimit.json", None)
    assert state is not None
    assert state["paused_until"] == resets_at + cfg.ratelimit_grace
    assert state["source_log"] == str(older_path)
    assert report.paused_until == state["paused_until"]


def test_all_stream_json_home_state_byte_identical_to_baseline(home, cfg):
    """A home where every key has exactly one (newest-and-only) stream-json log
    exercises the single-candidate loop body, unchanged by the multi-candidate
    fix -- its on-disk state must match the pre-fix baseline byte-for-byte."""
    cfg.ratelimit_grace = 60
    t0 = 12_000_000
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    resets_at = t0 + 500
    path = _write_stream_log(home, "T-1", t0, [_rate_limit_event("rejected", resets_at)])

    disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)

    state = store.read_json(home / "derived" / ".ratelimit.json", None)
    assert state is not None
    expected = {
        "paused_until": float(resets_at) + cfg.ratelimit_grace,
        "resets_at": float(resets_at),
        "rate_limit_type": "five_hour",
        "source_key": "T-1",
        "source_log": str(path),
        "ts": state["ts"],
    }
    expected_bytes = json.dumps(expected, indent=2, sort_keys=True).encode("utf-8")
    assert (home / "derived" / ".ratelimit.json").read_bytes() == expected_bytes


# ---------------------------------------------------------------------------
# AC5: real CLI surface (doctor / ratelimit / ratelimit --clear)
# ---------------------------------------------------------------------------

def _run(home, *argv, capsys=None):
    rc = main(["--home", str(home), *argv])
    cap = capsys.readouterr() if capsys else None
    out = cap.out if cap else ""
    try:
        return rc, json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return rc, out


def test_doctor_reports_rate_limit_clean_home(home, capsys):
    rc, out = _run(home, "doctor", capsys=capsys)
    assert rc == 0
    assert out["rate_limit"] == {"paused": False}


def test_doctor_and_ratelimit_verb_report_paused_state(home, cfg, capsys):
    # cmd_doctor/cmd_ratelimit read the pause against real wall-clock time, so the
    # dispatch sweeps here must anchor "now" to it too.
    t0 = store.now_epoch()
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    resets_at = t0 + 500
    _write_stream_log(home, "T-1", t0, [_rate_limit_event("rejected", resets_at)])
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)

    rc, out = _run(home, "doctor", capsys=capsys)
    assert rc == 0
    rl = out["rate_limit"]
    assert rl["paused"] is True
    assert rl["paused_until"] == resets_at + cfg.ratelimit_grace
    assert rl["resets_at"] == resets_at
    assert rl["rate_limit_type"] == "five_hour"
    assert "until" in rl and isinstance(rl["until"], str) and rl["until"]

    rc2, out2 = _run(home, "ratelimit", capsys=capsys)
    assert rc2 == 0
    assert out2 == rl


def test_ratelimit_clear_removes_state_and_next_sweep_spawns(home, cfg, capsys):
    t0 = store.now_epoch()
    _spawn_and_seed_ledger(home, cfg, "T-1", t0)
    _write_stream_log(home, "T-1", t0, [_rate_limit_event("rejected", t0 + 5000)])
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 10)
    assert (home / "derived" / ".ratelimit.json").exists()

    rc, out = _run(home, "ratelimit", "--clear", capsys=capsys)
    assert rc == 0
    assert out == {"cleared": True}
    assert not (home / "derived" / ".ratelimit.json").exists()

    _seed(home, "T-1", Phase.READY)
    report = disp.dispatch(cfg, DryRunSessions(), now=t0 + 11)
    assert "T-1" in report.spawned
