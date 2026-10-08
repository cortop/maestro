"""T-175: per-ticket hold -- storage, dispatcher gate, CLI, NEEDS-YOU, doctor."""
from __future__ import annotations

import io
import json
import sys

from conftest import run_doctor, seed_phase
from maestro import cli, dispatcher as disp, event_log, fleet, inbox, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from test_post_qa_skill import _qa_pass_to_awaiting_ci, _seed_ticket, _skill_spawns


def _cli(home, *argv):
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(home), *argv])
    finally:
        sys.stdout = old
    return code, buf.getvalue()


def _last_decision(home, key):
    return disp.key_decisions(home, key)[-1]


def test_hold_cli_roundtrip(home):
    seed_phase(home, "T-1", Phase.READY)
    code, _ = _cli(home, "hold", "T-1", "--for", "2h", "--reason", "pairing")
    assert code == 0
    path = home / "derived" / "holds" / "T-1.json"
    assert path.exists()
    code, out = _cli(home, "hold", "--list")
    listed = json.loads(out)
    assert code == 0 and listed["T-1"]["reason"] == "pairing" and listed["T-1"]["until"]
    assert _cli(home, "hold", "T-NOPE")[0] != 0
    assert _cli(home, "unhold", "T-1")[0] == 0
    assert not path.exists()
    assert "hold" not in disp.AGENT_TOOL_VERBS and "unhold" not in disp.AGENT_TOOL_VERBS


def test_held_key_never_spawns(home, cfg):
    seed_phase(home, "T-1", Phase.AWAITING_HUMAN)
    seed_phase(home, "T-2", Phase.READY)
    inbox.append_command(home, "T-1", "msg", {"text": "hi"})
    fleet.hold(home, "T-1", reason="mine")
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=store.now_epoch())
    assert _last_decision(home, "T-1")["outcome"] == "held"
    assert "T-1" not in report.spawned and "T-2" in report.spawned
    assert len(inbox.pending(home, "T-1")) == 1
    disp.dispatch(cfg, DryRunSessions(), now=store.now_epoch() + 1, key_filter=["T-1"])
    assert _last_decision(home, "T-1")["outcome"] == "held"
    assert len(inbox.pending(home, "T-1")) == 1


def test_hold_expiry_and_corrupt_file_fail_closed(home, cfg):
    seed_phase(home, "T-1", Phase.READY)
    now = store.now_epoch()
    fleet.hold(home, "T-1", until=now - 5)
    report = disp.dispatch(cfg, DryRunSessions(), now=now)
    assert not (home / "derived" / "holds" / "T-1.json").exists()
    assert "T-1" in report.spawned

    seed_phase(home, "T-2", Phase.READY)
    path = fleet.hold_path(home, "T-2")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    report = disp.dispatch(cfg, DryRunSessions(), now=now + 1)
    assert "T-2" not in report.spawned
    assert _last_decision(home, "T-2")["outcome"] == "held"


def test_held_key_skips_post_qa_skill(home, cfg):
    cfg.post_qa_skill = "/my-pr-polish"
    _seed_ticket(home, "T-1")
    _qa_pass_to_awaiting_ci(cfg, "T-1")
    fleet.hold(home, "T-1")
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=store.now_epoch())
    assert _skill_spawns(sessions) == []
    assert not any(e["type"] == "PostQaSkillSpawned" for e in event_log.read(home, "T-1"))


def test_held_keys_in_needs_you_and_doctor(home):
    seed_phase(home, "T-1", Phase.READY)
    code, report = run_doctor(home)
    check = next(c for c in report["checks"] if c["name"] == "holds")
    assert check["status"] == "ok"
    fleet.hold(home, "T-1", reason="pairing")
    assert _cli(home, "project")[0] == 0
    text = (home / "derived" / "NEEDS-YOU.md").read_text()
    assert "## Held" in text and "pairing" in text and "maestro unhold T-1" in text
    assert "Nothing is waiting on you" not in text
    _, report = run_doctor(home)
    check = next(c for c in report["checks"] if c["name"] == "holds")
    assert check["status"] == "warn" and "T-1" in check["detail"]
