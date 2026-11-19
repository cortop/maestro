"""Creating a ticket drafts acceptance criteria through `ops.suggest_acs` without being asked."""
from __future__ import annotations

import json
import subprocess

from maestro import cli, human_actions, ops, snapshot as snap_mod, store
from maestro import dispatcher as disp
from maestro.config import load
from maestro.sessions import DryRunSessions

SUGGESTED = ["first criterion", "second criterion", "third criterion"]


def _fake_claude(monkeypatch, suggestions=SUGGESTED, fail=None):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if fail is not None:
            raise fail
        out = json.dumps({"result": json.dumps(suggestions)})
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    monkeypatch.setattr(ops, "_suggest_run", run)
    return calls


def _spec(home, key):
    return store.spec_path(home, key).read_text(encoding="utf-8")


def _create_and_mint(home, *argv):
    assert cli.main(["--home", str(home), "create", *argv, "--no-nudge"]) == 0
    cfg = load(home)
    return disp.mint_new_tickets(cfg), cfg


def test_cli_create_without_acs_generates_checkbox_acs(home, monkeypatch):
    calls = _fake_claude(monkeypatch)
    keys, cfg = _create_and_mint(home, "Add a widget")
    spec = _spec(home, keys[0])
    assert len(calls) == 1
    for s in SUGGESTED:
        assert f"- [ ] {s}" in spec
    assert snap_mod.has_acs(spec)
    # a real sweep over the minted ticket still runs
    disp.dispatch(cfg, DryRunSessions(), store.now_epoch())


def test_user_supplied_acs_are_kept_verbatim_and_nothing_generated(home, monkeypatch):
    calls = _fake_claude(monkeypatch)
    keys, _ = _create_and_mint(home, "Add a widget", "--ac", "mine one", "--ac", "mine two")
    spec = _spec(home, keys[0])
    assert "- [ ] mine one\n- [ ] mine two\n" in spec
    assert "first criterion" not in spec
    assert calls == []


def test_generation_failure_still_creates_ticket_and_warns(home, monkeypatch, capsys):
    _fake_claude(monkeypatch, fail=subprocess.TimeoutExpired("claude", 120))
    keys, _ = _create_and_mint(home, "Add a widget")
    spec = _spec(home, keys[0])
    assert "## Acceptance criteria\n- \n" in spec
    assert not snap_mod.has_acs(spec)
    assert "could not auto-generate acceptance criteria" in capsys.readouterr().err


def test_human_actions_queue_ticket_generates_acs(home, monkeypatch):
    _fake_claude(monkeypatch)
    cfg = load(home)
    human_actions.queue_ticket(cfg, "From the UI")
    keys = disp.mint_new_tickets(cfg)
    assert "- [ ] first criterion" in _spec(home, keys[0])


def test_json_create_generates_acs(home, monkeypatch, capsys):
    _fake_claude(monkeypatch)
    assert cli.main(["--home", str(home), "create", "Sync one", "--json", "--no-nudge"]) == 0
    key = json.loads(capsys.readouterr().out)["key"]
    assert "- [ ] second criterion" in _spec(home, key)


def test_suggest_acs_action_still_works_unchanged(home, monkeypatch):
    _fake_claude(monkeypatch)
    cfg = load(home)
    human_actions.queue_ticket(cfg, "x")
    key = disp.mint_new_tickets(cfg)[0]
    assert ops.suggest_acs(cfg, key, run=ops._suggest_run) == SUGGESTED


def test_knob_off_skips_generation(home, monkeypatch):
    calls = _fake_claude(monkeypatch)
    (home / "config.toml").write_text("[maestro]\nauto_suggest_acs = false\n", encoding="utf-8")
    keys, _ = _create_and_mint(home, "Quiet")
    assert calls == [] and not snap_mod.has_acs(_spec(home, keys[0]))
