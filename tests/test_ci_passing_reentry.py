"""T-202: a duplicate passing CiObserved still routes a key sitting in awaiting-ci."""
import pytest

from maestro import dispatcher as disp
from maestro import event_log, providers, snapshot as snap_mod, store
from maestro.statemachine import Phase


class FakeVCS:
    """The only mock: the external GitHub boundary."""

    def __init__(self, status):
        self.status = status

    def pr_for_branch(self, branch, repo=None, env=None):
        return None

    def pr_status(self, pr_number, repo=None, env=None):
        return dict(self.status)

    def review_feedback(self, pr_number, repo=None, env=None):
        return []


def _status(ci_state, failing=()):
    return {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
            "ci_state": ci_state, "failing_checks": list(failing)}


def _seed(cfg, monkeypatch, status):
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": 0}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: FakeVCS(status))
    key = "T-1"
    store.atomic_write(store.spec_path(cfg.home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    h = disp.spec_hash_on_disk(cfg.home, key)
    event_log.append(cfg.home, key, "TicketCreated", {"title": key, "spec_hash": h}, actor="d")
    event_log.append(cfg.home, key, "PrOpened",
                     {"number": 42, "url": "https://github.com/x/y/pull/42", "draft": False},
                     actor="r")
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": Phase.AWAITING_CI.value}, actor="r")
    snap_mod.rebuild(cfg.home, key)
    return key


def _types(cfg, key):
    return [e["type"] for e in event_log.read(cfg.home, key)]


def _back_to_awaiting_ci(cfg, key):
    event_log.append(cfg.home, key, "PhaseChanged",
                     {"phase": Phase.AWAITING_CI.value, "reason": "no-push round"}, actor="r")
    snap_mod.rebuild(cfg.home, key)


def test_reentry_routes_to_in_review(cfg, monkeypatch):
    key = _seed(cfg, monkeypatch, _status("passing"))
    disp.sync_vcs(cfg, now=1000)
    assert snap_mod.load(cfg.home, key).phase == Phase.IN_REVIEW.value
    _back_to_awaiting_ci(cfg, key)
    before = _types(cfg, key).count("CiObserved")

    disp.sync_vcs(cfg, now=2000)

    assert snap_mod.load(cfg.home, key).phase == Phase.IN_REVIEW.value
    assert _types(cfg, key).count("CiObserved") == before == 1


@pytest.mark.parametrize("state,failing", [("pending", ()), ("failing", ("unit",))])
def test_duplicate_non_passing_noop(cfg, monkeypatch, state, failing):
    key = _seed(cfg, monkeypatch, _status(state, failing))
    disp.sync_vcs(cfg, now=1000)
    _back_to_awaiting_ci(cfg, key)
    n = len(event_log.read(cfg.home, key))
    disp.sync_vcs(cfg, now=2000)
    assert len(event_log.read(cfg.home, key)) == n
    assert snap_mod.load(cfg.home, key).phase == Phase.AWAITING_CI.value


def test_second_sweep_appends_nothing(cfg, monkeypatch):
    key = _seed(cfg, monkeypatch, _status("passing"))
    disp.sync_vcs(cfg, now=1000)
    _back_to_awaiting_ci(cfg, key)
    disp.sync_vcs(cfg, now=2000)
    n = len(event_log.read(cfg.home, key))
    disp.sync_vcs(cfg, now=3000)
    assert len(event_log.read(cfg.home, key)) == n
