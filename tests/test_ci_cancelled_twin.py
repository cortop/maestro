"""T-201: a CANCELLED run is ignored when a same-named run on the same head succeeded."""
import json

from maestro.providers import cli
from maestro.providers.cli import GitHubCliVCS


def _vcs(monkeypatch, rollup):
    calls = []

    def fake_run(cmd, timeout=60, env=None):
        calls.append(cmd)
        return 0, json.dumps({"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "sha1",
                              "statusCheckRollup": rollup, "isDraft": False}), ""

    monkeypatch.setattr(cli, "_run", fake_run)
    return GitHubCliVCS({"repos": ["o/r"]}), calls


def test_cancelled_twin_ignored(monkeypatch):
    vcs, _ = _vcs(monkeypatch, [
        {"name": "assign-reviewers", "conclusion": "CANCELLED",
         "detailsUrl": "https://github.com/o/r/actions/runs/1"},
        {"name": "assign-reviewers", "conclusion": "SUCCESS",
         "detailsUrl": "https://github.com/o/r/actions/runs/2"},
    ])
    st = vcs.pr_status(5)
    assert st["ci_state"] == "passing"
    assert st["failing_checks"] == []
    # reruns stay consistent: nothing to rerun for a cancelled twin of a passing run
    assert vcs.rerun_failed(5, "sha1") == {"ok": False}


def test_lone_cancelled_fails(monkeypatch):
    vcs, _ = _vcs(monkeypatch, [
        {"name": "assign-reviewers", "conclusion": "CANCELLED",
         "detailsUrl": "https://github.com/o/r/actions/runs/1"},
        {"name": "build", "conclusion": "SUCCESS"},
    ])
    st = vcs.pr_status(5)
    assert st["ci_state"] == "failing"
    assert st["failing_checks"] == ["assign-reviewers"]
    assert vcs.rerun_failed(5, "sha1") == {"ok": True, "run_ids": ["1"]}
