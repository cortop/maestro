"""T-251: the review round -- tests, findings, brief, handoff to the human.

Real git, real CLI and real dispatcher sweeps; only the code-host read (`pr_head`) and the
`claude -p` spawn (`DryRunSessions`) are faked. Fixtures are synthetic.
"""
import os

import pytest

from conftest import make_origin_and_repo
from test_review_kind import KEY, URL, FakeVCS, _push_pr_head, _review
from maestro import claims, cli, dispatcher as disp, event_log, ops, snapshot as snap_mod, store
from maestro import config
from maestro.sessions import DryRunSessions


@pytest.fixture
def board(home, tmp_path, monkeypatch):
    origin, repo = make_origin_and_repo(tmp_path, name="widgets")
    (home / "config.toml").write_text(
        f'[maestro]\ntest_command = "true"\n[repos.widgets]\npath = "{repo}"\n'
        'slug = "acme/widgets"\n')
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    sha = _push_pr_head(origin, repo, tmp_path, "pr-one")
    assert _review(home, FakeVCS(sha), "--source", "https://chat.example/p/1",
                   monkeypatch=monkeypatch) == 0
    return home, sha


def _sweep(home, now):
    sessions = DryRunSessions()
    disp.dispatch(config.load(str(home)), sessions, now=now)
    return sessions


def _to_qa(home):
    """Drive the real sweep until the review ticket reaches `qa`."""
    for i in range(100):
        if snap_mod.load(home, KEY).phase == "qa":
            return
        _sweep(home, 1000 + i * 1000)
        claim = claims.read_claim(home, KEY)
        if claim and claim.get("kind") == "testrun":
            try:  # this process is the detached run's parent: reap it so the next sweep folds it
                os.waitpid(int(claim["pid"]), 0)
            except (ChildProcessError, ValueError, TypeError):
                pass
    assert snap_mod.load(home, KEY).phase == "qa"


def _finding(severity="blocking", line=3, comment="Please guard the empty case."):
    return cli.main(["review-finding", KEY, "--file", "src/a.py", "--line", str(line),
                     "--severity", severity, "--comment", comment])


def _brief(verdict="request-changes"):
    return cli.main(["review-brief", KEY, "--summary", "Adds a parser.",
                     "--checked", "Tests passed; claims match the diff (weaker than a spec).",
                     "--decisions", "Is the new public API acceptable?", "--verdict", verdict,
                     "--reading-order", "src/a.py then tests/test_a.py"])


def test_round_runs_tests_then_qa_with_review_skill_and_no_ac_gate(board):
    home, sha = board
    _to_qa(home)
    snap = snap_mod.load(home, KEY)
    assert snap.test_runs and all(r["passed"] for r in snap.test_runs.values())  # dispatcher ran the repo's test_command
    sessions = _sweep(home, 20000)
    assert disp.resolve_reconcile_command(config.load(str(home)), "qa", "review").endswith("-review")
    assert [s for s in sessions.spawned if KEY in str(s)]  # the review reconciler is spawned


def test_failed_tests_are_a_finding_not_a_bounce_to_implementing(board, monkeypatch):
    home, sha = board
    cfg_path = home / "config.toml"
    cfg_path.write_text(cfg_path.read_text().replace('test_command = "true"', 'test_command = "false"'))
    _to_qa(home)
    phases = [e["payload"].get("phase") for e in event_log.read(home, KEY) if e["type"] == "PhaseChanged"]
    assert "implementing" not in phases
    assert [r["passed"] for r in snap_mod.load(home, KEY).test_runs.values()] == [False]


def test_findings_and_brief_are_structured_and_hand_off_to_the_human(board, capsys):
    home, sha = board
    _to_qa(home)
    assert _finding() == 0 and _finding("question", 9, "Why this default?") == 0
    assert _finding() == 0  # same finding twice: recorded once
    assert _brief() == 0

    snap = snap_mod.load(home, KEY)
    assert snap.review_comments == [
        {"file": "src/a.py", "line": 3, "severity": "blocking", "comment": "Please guard the empty case."},
        {"file": "src/a.py", "line": 9, "severity": "question", "comment": "Why this default?"}]
    assert snap.review_brief["verdict"] == "request-changes"
    assert snap.review_brief["head_sha"] == sha
    assert snap.phase == "awaiting-human" and snap.open_questions

    text = ops.review_brief_path(home, KEY).read_text()
    for heading in ("What the PR does", "What was checked", "Findings", "Decisions for you",
                    "Suggested verdict", "Reading order"):
        assert heading in text
    assert URL in text and "https://chat.example/p/1" in text and "weaker" in text
    assert "src/a.py:3" in text

    # a woken ticket with a brief at this head does not start another round
    assert disp._start_review_round(config.load(str(home)), KEY, snap) is None


@pytest.mark.parametrize("args", [
    ["--file", "a.py", "--line", "0", "--severity", "nit", "--comment", "x"],
    ["--file", "a.py", "--line", "1", "--severity", "nit", "--comment", "  "],
    ["--file", " ", "--line", "1", "--severity", "nit", "--comment", "x"],
])
def test_bad_findings_refuse(board, args):
    home, _ = board
    _to_qa(home)
    assert cli.main(["review-finding", KEY, *args]) != 0
    assert snap_mod.load(home, KEY).review_comments == []


def test_verbs_refuse_outside_qa_and_on_non_review_tickets(board, home_factory=None):
    home, _ = board
    assert _finding() != 0  # still ready, not qa
    assert _brief() != 0
    assert snap_mod.load(home, KEY).review_brief is None


def test_review_session_cannot_edit_merge_or_post(board):
    home, _ = board
    denied = disp.MERGE_DENYLIST + disp.phase_denylist("qa", "review")
    assert {"Edit", "Write", "Bash(gh pr merge:*)", "Bash(gh pr comment:*)",
            "Bash(gh pr review:*)", "Bash(gh api:*)"} <= set(denied)
    assert "Edit" in disp.phase_denylist("qa")  # the plain qa restrictions are unchanged
    assert "Bash(gh pr comment:*)" not in disp.phase_denylist("qa")
    granted = disp.phase_verb_grant("qa", "review")
    assert "Bash(maestro review-brief:*)" in granted and "Bash(maestro set-phase:*)" not in granted


def test_large_diff_asks_before_spending_a_round(board):
    home, sha = board
    cfg_path = home / "config.toml"
    cfg_path.write_text(cfg_path.read_text().replace('[maestro]\n', '[maestro]\npr_split_threshold = 1\n'))
    wt = store.worktree_path(home, KEY)
    (wt / "big.txt").write_text("\n".join(f"line {i}" for i in range(20)))
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "big"],
                   cwd=wt, check=True)
    _sweep(home, 1000)
    snap = snap_mod.load(home, KEY)
    assert snap.phase == "awaiting-human" and snap.open_questions
    assert snap.review_comments == [] and not snap.test_runs
