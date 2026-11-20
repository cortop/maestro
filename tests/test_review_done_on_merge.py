"""M-4: a review-kind ticket whose PR merged, or that the human already reviewed, goes to `done`.

Real CLI, real `human_actions`, real dispatcher sweeps over a temp home; only the code-host
boundary (`FakeVCS`) and the `claude -p` spawn (`DryRunSessions`) are faked. Fixtures are synthetic.
"""
from __future__ import annotations

import pytest

from conftest import seed_review_ticket
from maestro import cli, config, event_log, human_actions, projection, providers
from maestro import dispatcher as disp
from maestro import snapshot as snap_mod
from maestro.sessions import DryRunSessions

KEY = "T-60"


class FakeVCS:
    def __init__(self, state="OPEN"):
        self.state = state

    def pr_status(self, pr_number, repo=None, env=None):
        return {"state": self.state, "mergeable": "MERGEABLE", "head_sha": "a" * 40,
                "ci_state": "passing", "failing_checks": [], "draft": False}


@pytest.fixture
def board(home, monkeypatch):
    (home / "config.toml").write_text('[providers]\nvcs = "github_cli"\n')
    vcs = FakeVCS()
    monkeypatch.setattr(providers, "get_vcs", lambda cfg: vcs)
    return home, vcs


def _sweep(home, now, sessions=None):
    # the fixture's placeholder spec hash would otherwise wake the ticket
    event_log.append(home, KEY, "SpecObserved", {"spec_hash": disp.spec_hash_on_disk(home, KEY)},
                     actor="d")
    snap_mod.rebuild(home, KEY)
    disp.dispatch(config.load(str(home)), sessions or DryRunSessions(), now=now)
    return snap_mod.load(home, KEY)


@pytest.mark.parametrize("phase", ["qa", "awaiting-human"])
def test_merged_pr_moves_review_ticket_to_done(board, phase):
    home, vcs = board
    seed_review_ticket(home, phase=phase)
    vcs.state = "MERGED"
    snap = _sweep(home, 10_000)
    assert snap.phase == "done"
    last = [e for e in event_log.read(home, KEY) if e["type"] == "PhaseChanged"][-1]
    assert last["payload"]["phase"] == "done" and last["actor"] == "dispatcher"


def test_human_reviewed_moves_review_ticket_to_done(home, capsys):
    seed_review_ticket(home)
    assert cli.main(["--home", str(home), "reviewed", KEY]) == 0
    assert snap_mod.load(home, KEY).phase == "done"


def test_reviewed_refuses_a_non_review_ticket(home):
    from conftest import seed_ticket
    from maestro import store
    seed_ticket(home, "T-70", "impl")
    with pytest.raises(store.MaestroError, match="only a review ticket"):
        human_actions.mark_reviewed(config.Config(home=home), "T-70")


def test_open_unreviewed_review_ticket_stays_put(board):
    home, vcs = board
    seed_review_ticket(home)
    assert _sweep(home, 10_000).phase == "awaiting-human"


def test_done_transition_is_idempotent(board):
    home, vcs = board
    seed_review_ticket(home)
    vcs.state = "MERGED"
    _sweep(home, 10_000)
    n = len(event_log.read(home, KEY))
    assert _sweep(home, 20_000).phase == "done"
    human_actions.mark_reviewed(config.load(str(home)), KEY)
    done = [e for e in event_log.read(home, KEY)
            if e["type"] == "PhaseChanged" and e["payload"]["phase"] == "done"]
    assert len(done) == 1
    assert len(event_log.read(home, KEY)) == n + 1  # only the second sweep's SpecObserved


def test_done_review_ticket_not_spawned_or_in_needs_you(board):
    home, vcs = board
    seed_review_ticket(home)
    vcs.state = "MERGED"
    sessions = DryRunSessions()
    assert _sweep(home, 10_000, sessions).phase == "done"
    assert KEY not in [getattr(s, "key", s) if not isinstance(s, tuple) else s[0]
                       for s in sessions.spawned]
    assert KEY not in projection.render(home)["NEEDS-YOU.md"]
