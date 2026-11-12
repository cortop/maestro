"""Fleet view-model panels: sessions, doctor, backups, schedule."""
from __future__ import annotations

import json
import os

from conftest import seed_phase
from maestro import claims, store
from maestro.statemachine import Phase
from maestro.view import fleet as fv


def _log(home, key, records, epoch="1700000000.0"):
    d = home / "agent-logs" / key
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"reconcile-{key}-{epoch}.stream.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return p


def test_session_rows_cost_present_and_absent(home, cfg):
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    seed_phase(home, "T-2", Phase.IMPLEMENTING)
    for k in ("T-1", "T-2"):
        claims.write_claim(home, k, os.getpid(), "r")
    _log(home, "T-1", [{"type": "system", "subtype": "init", "model": "m"},
                       {"type": "result", "subtype": "success", "is_error": False,
                        "total_cost_usd": 0.42, "num_turns": 3}])
    _log(home, "T-2", [{"type": "system", "subtype": "init", "model": "m"}])
    rows = {r["key"]: r for r in fv.session_rows(home, cfg, store.now_epoch())}
    assert rows["T-1"]["cost"] == 0.42
    assert rows["T-2"]["cost"] is None
    assert rows["T-1"]["runner"] == "claude" and rows["T-1"]["pid"] == os.getpid()
    assert rows["T-1"]["round"] and rows["T-1"]["age_s"] is not None
    assert "tool" in rows["T-1"]


def test_session_rows_skips_stale_claim(home, cfg):
    seed_phase(home, "T-3", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-3", 99999999, "r")
    assert fv.session_rows(home, cfg) == []


def test_doctor_rows_problems_first(home, cfg):
    rows = fv.doctor_rows(cfg, store.now_epoch())
    ranks = [{"fail": 0, "warn": 1}.get(r["status"], 2) for r in rows]
    assert rows and ranks == sorted(ranks)


def test_backup_panel(home, cfg):
    cfg.backup_interval, cfg.backup_retention = 600, 5
    empty = fv.backup_panel(cfg)
    assert empty["last"] is None and empty["next"] is None and empty["retention"] == 5
    from maestro import backup
    d = backup.resolve_backup_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{backup._PREFIX}{'20240102T030405Z'}{backup._SUFFIX}"
    (d / name).write_bytes(b"x")
    panel = fv.backup_panel(cfg)
    if panel["last"] is not None:
        assert panel["next"] == panel["last"] + 600
    assert panel["count"] == 1 and panel["interval"] == 600


def test_schedule_rows_next_due(home, cfg):
    cfg.scheduled = [{"name": "tick", "prompt": "p", "every": "1h"},
                     {"name": "off", "prompt": "p", "every": "1h", "enabled": False}]
    rows = {r["name"]: r for r in fv.schedule_rows(cfg, 5000.0)}
    assert rows["tick"]["next_due"] == 3600
    assert rows["off"]["next_due"] is None
