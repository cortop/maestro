"""T-203: a key `_observe_ci` routed away in a sweep gets no review route in that same sweep."""
from maestro import dispatcher as disp
from maestro import event_log, snapshot as snap_mod
from maestro.statemachine import Phase

from tests.test_undraft import FakeVCS, _seed, _use_fake

FAILING = {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
           "ci_state": "failing", "failing_checks": ["unit"], "draft": True}
REVIEW = [{"id": "rc-1", "state": "CHANGES_REQUESTED", "body": "nit", "author": "reviewer1"}]


def _phase_changes(cfg, key):
    return [e for e in event_log.read(cfg.home, key) if e["type"] == "PhaseChanged"]


def test_no_double_route(cfg, monkeypatch):
    fake = FakeVCS(FAILING, reviews=REVIEW)
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.AWAITING_CI)
    before = len(_phase_changes(cfg, "T-5"))

    disp.sync_vcs(cfg, now=1000)

    assert len(_phase_changes(cfg, "T-5")) == before + 1
    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IMPLEMENTING.value


def test_review_handled_next_sweep(cfg, monkeypatch):
    fake = FakeVCS(FAILING, reviews=REVIEW)
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.AWAITING_CI)
    disp.sync_vcs(cfg, now=1000)
    assert not [e for e in event_log.read(cfg.home, "T-5") if e["type"] == "ReviewFeedbackReceived"]

    # The fix lands, CI is green, the key returns to awaiting-ci: the pending
    # review is observed and routed on this sweep.
    fake.status = dict(FAILING, ci_state="passing", failing_checks=[])
    event_log.append(cfg.home, "T-5", "PhaseChanged", {"phase": Phase.AWAITING_CI.value}, actor="r")
    snap_mod.rebuild(cfg.home, "T-5")
    disp.sync_vcs(cfg, now=2000)

    assert [e for e in event_log.read(cfg.home, "T-5") if e["type"] == "ReviewFeedbackReceived"]
    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IMPLEMENTING.value
