"""Tests for maestro.human_actions: the shared human-action surface (T-237)."""
from __future__ import annotations

import pytest

from maestro import cli, fleet, human_actions, inbox, store
from maestro.config import Config
from maestro.statemachine import Phase
from tests.conftest import seed_phase


def _cmds(home, key):
    return inbox.pending(home, key)


def test_answer_records_accept_marker_and_nudges(tmp_path, monkeypatch):
    home = tmp_path
    seed_phase(home, "T-1")
    calls = []
    human_actions.answer(home, "T-1", "yes", qid="q1", accepted_recommendation=True,
                         nudge=calls.append)
    human_actions.answer(home, "T-1", "yes", qid="q2")
    cmds = _cmds(home, "T-1")
    assert cmds[0]["args"] == {"text": "yes", "qid": "q1", "accepted_recommendation": True}
    assert "accepted_recommendation" not in cmds[1]["args"]
    assert calls == ["T-1"]  # nudge is opt-in: the second call had none


def test_message_and_degraded_only_guards(tmp_path):
    home = tmp_path
    seed_phase(home, "T-1")
    human_actions.message(home, "T-1", "hello")
    for fn in (human_actions.retry, human_actions.discard):
        with pytest.raises(store.MaestroError, match="degraded"):
            fn(home, "T-1")
    seed_phase(home, "T-2", Phase.DEGRADED)
    human_actions.retry(home, "T-2")
    human_actions.discard(home, "T-2")
    assert [c["command"] for c in _cmds(home, "T-2")] == ["retry", "discard"]


def test_hold_release_refuse_unknown_ticket(tmp_path):
    home = tmp_path
    with pytest.raises(store.MaestroError, match="unknown ticket"):
        human_actions.hold(home, "T-9")
    with pytest.raises(store.MaestroError, match="unknown ticket"):
        human_actions.release(home, "T-9")
    seed_phase(home, "T-1")
    human_actions.hold(home, "T-1", reason="x")
    assert fleet.hold_path(home, "T-1").exists()
    human_actions.release(home, "T-1")
    assert not fleet.hold_path(home, "T-1").exists()


def test_pause_resume(tmp_path):
    human_actions.pause(tmp_path, reason="r")
    assert fleet.pause_state(tmp_path, store.now_epoch()) is not None
    human_actions.resume(tmp_path)
    assert fleet.pause_state(tmp_path, store.now_epoch()) is None


def test_stop_without_claim_raises(tmp_path):
    seed_phase(tmp_path, "T-1")
    with pytest.raises(store.MaestroError) as ei:
        human_actions.stop(Config(home=tmp_path), "T-1")
    assert ei.value.result["stopped"] is False


def test_cli_verbs_exit_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path))
    seed_phase(tmp_path, "T-1")
    assert cli.main(["ans", "T-1", "ok", "--qid", "q", "--no-nudge"]) == 0
    assert _cmds(tmp_path, "T-1")[0]["args"] == {"text": "ok", "qid": "q"}
    assert cli.main(["hold", "T-1"]) == 0
    assert cli.main(["unhold", "T-1"]) == 0
    assert cli.main(["hold", "T-404"]) == 1
    assert cli.main(["unhold", "T-404"]) == 1
    assert cli.main(["stop", "T-1"]) == 1
    assert cli.main(["fleet", "pause"]) == 0
    assert cli.main(["fleet", "resume"]) == 0
