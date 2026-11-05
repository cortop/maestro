"""`maestro brief`: classifies the implementing round from real phase events and prints only that round's instructions."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import seed_phase
from maestro import dispatcher as disp
from maestro import event_log, implementing_brief, store
from maestro import snapshot as snap_mod
from maestro.cli import main as cli_main
from maestro.sessions import PiCliSessions
from maestro.statemachine import Phase

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "commands" / "maestro-reconcile-implementing.md"
ROUND_HEADINGS = {r: f"## Round: {r} " for r in implementing_brief.ROUNDS}


def _enter(home, key, prev, reason, *, pr=None):
    """Move *key* through *prev* and into implementing with *reason*, via real events."""
    event_log.append(home, key, "PhaseChanged", {"phase": prev, "reason": "x"}, actor="r")
    if pr is not None:
        event_log.append(home, key, "PrOpened", {"number": pr, "url": f"https://x/{pr}", "draft": True}, actor="r")
    event_log.append(home, key, "PhaseChanged", {"phase": "implementing", "reason": reason}, actor="r")
    snap_mod.rebuild(home, key)


def _brief(home, key, capsys, *extra):
    capsys.readouterr()
    assert cli_main(["--home", str(home), "brief", key, *extra]) == 0
    out = capsys.readouterr().out
    head = json.loads(out.split("```json\n", 1)[1].split("\n```", 1)[0])
    return head, out


CASES = [
    ("fresh", "ready", "worktree ready"),
    ("qa-fix", "qa", "qa: AC 1 failed -- evidence"),
    ("ci-fix", "awaiting-ci", "CI failing: lint"),
    ("ci-fix", "awaiting-ci", "PR #7: CI failing: lint"),
    ("review-feedback", "in-review", "changes requested: please rename"),
    ("review-feedback", "awaiting-ci", "approved with comments: a.py:3: nit"),
    ("conflict", "awaiting-ci", "resolve merge conflict for PR #7"),
    ("base-drift", "awaiting-ci", "origin/main advanced (policy=always) — sync worktree with latest main"),
    ("split-decision", "awaiting-human", "pr split decision: approve"),
]


@pytest.mark.parametrize("expected,prev,reason", CASES)
def test_brief_classifies_each_round(home, capsys, expected, prev, reason):
    seed_phase(home, "T-1", Phase.READY)
    _enter(home, "T-1", prev, reason)
    head, out = _brief(home, "T-1", capsys)
    assert head["round"] == expected
    assert head["reason"] == reason
    instructions = out.split("# Instructions", 1)[1]
    assert ROUND_HEADINGS[expected] in instructions
    for other, heading in ROUND_HEADINGS.items():
        if other != expected:
            assert heading not in instructions
    # sync steps accompany the rounds that sync, and only those
    assert ("## Sync with the base branch" in instructions) == (expected in ("conflict", "base-drift"))


def test_latest_transition_wins(home, capsys):
    seed_phase(home, "T-1", Phase.READY)
    _enter(home, "T-1", "ready", "worktree ready")
    _enter(home, "T-1", "qa", "qa: AC 1 failed")
    head, _ = _brief(home, "T-1", capsys)
    assert head["round"] == "qa-fix"


def test_round_override_prints_other_instructions(home, capsys):
    seed_phase(home, "T-1", Phase.READY)
    _enter(home, "T-1", "awaiting-ci", "CI failing: x")
    head, out = _brief(home, "T-1", capsys, "--round", "sync")
    assert head["round"] == "ci-fix" and head["instructions_for"] == "sync"
    assert "## Sync with the base branch" in out and "## Round:" not in out


@pytest.mark.parametrize("prev,reason", [
    ("awaiting-human", "human: please do something else"),
    ("awaiting-human", "approved scope"),
    ("verifying", "tests failed: 2 red"),
])
def test_unknown_reason_gets_all_instructions(home, capsys, prev, reason):
    seed_phase(home, "T-1", Phase.READY)
    _enter(home, "T-1", prev, reason)
    head, out = _brief(home, "T-1", capsys)
    assert head["round"] == "unknown"
    instructions = out.split("# Instructions", 1)[1]
    for heading in ROUND_HEADINGS.values():
        assert heading in instructions
    assert "## Sync with the base branch" in instructions


def test_brief_replaces_preamble(home, capsys):
    seed_phase(home, "T-1", Phase.READY)
    _enter(home, "T-1", "ready", "worktree ready")
    head, out = _brief(home, "T-1", capsys)
    # env fields the old two `maestro env` calls returned
    for field in ("repo_path", "slug", "base_branch", "branch_prefix", "mode", "display_key",
                  "stack_tool", "allowed_paths", "reconcile_command"):
        assert field in head["env"]
    assert "home" in head["home_env"] and "qa_standards_axis" in head["home_env"]
    assert head["snapshot"]["key"] == "T-1" and head["snapshot"]["phase"] == "implementing"
    assert "inbox_pending" in head["snapshot"]
    assert head["spec_hash"] == head["snapshot"]["spec_hash"]
    assert "## Acceptance criteria" in out.split("# Spec", 1)[1].split("# Context", 1)[0]
    dossier = out.split("# Context", 1)[1].split("# Instructions", 1)[0]
    assert "## Phase history" in dossier and "worktree ready" in dossier
    # the skill's preamble is that single call
    text = SKILL.read_text(encoding="utf-8")
    preamble = text.split("## Always: load state first", 1)[1].split("##", 1)[0]
    assert 'maestro brief "$KEY"' in preamble
    for old in ('maestro env', 'maestro observe-spec', 'maestro snapshot'):
        assert old not in preamble.split("```bash", 1)[1].split("```", 1)[0]


def test_brief_granted_for_every_runner(home, tmp_path):
    # claude: the phase grant rendered into --allowedTools
    assert "Bash(maestro brief:*)" in disp.phase_verb_grant("implementing")
    assert "Bash(maestro brief:*)" not in disp.phase_verb_grant("qa")
    # opencode: the installed implementing agent stub
    repo = tmp_path / "acme"
    (home / "config.toml").write_text(f'[repos.acme]\npath = "{repo}"\n[maestro]\nrunner_enabled = ["claude", "opencode"]\n')
    assert cli_main(["--home", str(home), "install-commands", "--repo", "acme"]) == 0
    stub = (repo / ".opencode" / "agent" / "maestro-reconcile-implementing.md").read_text()
    assert '"maestro brief *": allow' in stub
    # pi: the guard data baked at spawn
    sess = PiCliSessions(home=home, clock=lambda: 1_000_000.0, capture_session_logs=False)
    proc = MagicMock()
    proc.pid = os.getpid()
    with patch("subprocess.Popen", return_value=proc):
        sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home,
                   allowed_tools=disp.phase_verb_grant("implementing"), runner_model="glm-5.2")
    data = json.loads((store.pi_agent_key_dir(store.pi_agent_dir(home), "T-1")
                       / "extensions" / "pi_guard_data.json").read_text())
    assert "brief" in data["allowed_verbs"]


def test_fresh_round_byte_budget():
    core = SKILL.stat().st_size
    fresh = len(implementing_brief.instructions("fresh").encode())
    assert core + fresh <= 15_000, (core, fresh)
    from test_harness_surface import SKILL_BYTE_CEILINGS
    assert SKILL_BYTE_CEILINGS["implementing"] <= core + 500  # the ceiling tracks the core skill
