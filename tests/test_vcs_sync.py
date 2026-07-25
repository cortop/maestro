"""Dispatcher-owned PR observation: `sync_vcs` polls PR state, CI checks, and
review comments via the configured `vcs` provider (opt-in, cursor-gated exactly
like `sync_external_sources`) and advances awaiting-ci/in-review tickets directly
— merged finalizes, a CONFLICTING PR routes to implementing, failing CI routes to
implementing with the failing check names, passing CI moves awaiting-ci to
in-review, and a CHANGES_REQUESTED review routes back to implementing with the
verbatim comment body. Retires the reconciler's own `gh pr checks` shelling.
"""
import json

from maestro import dispatcher as disp
from maestro import event_log, providers, snapshot as snap_mod, store
from maestro.cli import main
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase, SLEEPING_PHASES


class FakeVCS:
    """The only mock: the external GitHub boundary (the VCS Protocol seam)."""

    def __init__(self, statuses=None, reviews=None):
        self.statuses = statuses or {}
        self.reviews = reviews or {}
        self.status_calls: list[int] = []
        self.review_calls: list[int] = []

    def pr_for_branch(self, branch):
        return None

    def pr_status(self, pr_number: int) -> dict:
        self.status_calls.append(pr_number)
        return self.statuses.get(pr_number, {
            "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
            "ci_state": "unknown", "failing_checks": [],
        })

    def review_feedback(self, pr_number: int) -> list[dict]:
        self.review_calls.append(pr_number)
        return self.reviews.get(pr_number, [])


def _seed(cfg, key, phase, pr=42):
    store.atomic_write(store.spec_path(cfg.home, key), f"# {key}\napproval_tier: 0\n")
    spec_hash = disp.spec_hash_on_disk(cfg.home, key)
    event_log.append(cfg.home, key, "TicketCreated",
                     {"title": key, "spec_hash": spec_hash}, actor="d")
    event_log.append(cfg.home, key, "PrOpened",
                     {"number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": False},
                     actor="r")
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(cfg.home, key)


def _use_fake(cfg, monkeypatch, fake, *, interval=0):
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": interval}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)


def test_vcs_none_by_default_skips_sync(cfg):
    """Default config (vcs="none") leaves sync a byte-for-byte no-op."""
    result = disp.sync_vcs(cfg, now=1000)
    assert result == {"checked": 0}
    assert not (cfg.home / "derived" / ".vcs_cursor.json").exists()


def test_sync_vcs_cursor_gates_repeated_polls(cfg, monkeypatch):
    fake = FakeVCS()
    _use_fake(cfg, monkeypatch, fake, interval=900)
    _seed(cfg, "T-5", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)
    assert fake.status_calls == [42]
    disp.sync_vcs(cfg, now=1100)  # within the 900s window -> no re-poll
    assert fake.status_calls == [42]
    disp.sync_vcs(cfg, now=2000)  # past the window -> polls again
    assert fake.status_calls == [42, 42]


def test_sync_vcs_failing_ci_routes_to_implementing_with_check_names(cfg, monkeypatch):
    fake = FakeVCS(statuses={42: {
        "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
        "ci_state": "failing", "failing_checks": ["lint", "unit"],
    }})
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.IMPLEMENTING.value
    assert snap.failing_checks == ["lint", "unit"]

    evs = event_log.read(cfg.home, "T-5")
    ci = [e for e in evs if e["type"] == "CiObserved"]
    assert len(ci) == 1
    assert ci[0]["payload"]["failing_checks"] == ["lint", "unit"]
    assert "lint" in ci[0]["payload"]["detail"] and "unit" in ci[0]["payload"]["detail"]

    changed = [e for e in evs if e["type"] == "PhaseChanged"
               and e["payload"].get("phase") == Phase.IMPLEMENTING.value]
    assert changed and "lint" in changed[-1]["payload"]["reason"]


def test_sync_vcs_ci_observed_is_idempotent_on_unchanged_state(cfg, monkeypatch):
    """Re-running the tick with unchanged CI (same head SHA + check-run set)
    appends zero new events — the step-id is a pure function of that content."""
    fake = FakeVCS(statuses={42: {
        "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
        "ci_state": "failing", "failing_checks": ["unit"],
    }})
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)
    n_before = len(event_log.read(cfg.home, "T-5"))

    # Ticket is now `implementing` (an active phase, not awaiting-ci/in-review),
    # so re-seed it back into awaiting-ci with the same PR to simulate the tick
    # observing the identical CI result on a later sweep.
    from maestro import ops
    ops.set_phase(cfg, "T-5", Phase.AWAITING_CI, reason="re-check")
    n_after_phase = len(event_log.read(cfg.home, "T-5"))

    disp.sync_vcs(cfg, now=2000)
    evs = event_log.read(cfg.home, "T-5")
    ci_events = [e for e in evs if e["type"] == "CiObserved"]
    assert len(ci_events) == 1  # no duplicate CiObserved for the same head_sha/state/checks
    assert len(evs) == n_after_phase  # no new events at all this tick (still failing -> already implementing... )


def test_sync_vcs_passing_ci_moves_awaiting_ci_to_in_review(cfg, monkeypatch):
    fake = FakeVCS(statuses={42: {
        "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha2",
        "ci_state": "passing", "failing_checks": [],
    }})
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)
    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IN_REVIEW.value


def test_sync_vcs_merged_pr_finalizes(cfg, monkeypatch):
    fake = FakeVCS(statuses={42: {
        "state": "MERGED", "mergeable": "UNKNOWN", "head_sha": "sha3",
        "ci_state": "passing", "failing_checks": [],
    }})
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.IN_REVIEW)

    disp.sync_vcs(cfg, now=1000)
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.DONE.value
    assert snap.pr_state == "merged"


def test_sync_vcs_conflicting_pr_routes_to_implementing(cfg, monkeypatch):
    fake = FakeVCS(statuses={42: {
        "state": "OPEN", "mergeable": "CONFLICTING", "head_sha": "sha4",
        "ci_state": "unknown", "failing_checks": [],
    }})
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)
    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IMPLEMENTING.value
    # a conflicting PR doesn't also get a (misleading) CiObserved this tick
    assert not [e for e in event_log.read(cfg.home, "T-5") if e["type"] == "CiObserved"]


def test_sync_vcs_changes_requested_review_routes_to_implementing(cfg, monkeypatch):
    fake = FakeVCS(
        statuses={42: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha5",
                       "ci_state": "passing", "failing_checks": []}},
        reviews={42: [{"id": "rc-1", "state": "CHANGES_REQUESTED",
                      "body": "please rename this variable", "author": "reviewer1"}]},
    )
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.IN_REVIEW)

    disp.sync_vcs(cfg, now=1000)
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.IMPLEMENTING.value

    # verbatim comment body lands in the event log (via `maestro events`)
    out = main(["--home", str(cfg.home), "events", "T-5"])
    assert out == 0


def test_review_feedback_verbatim_body_via_maestro_events(cfg, monkeypatch, capsys):
    fake = FakeVCS(
        statuses={42: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha5",
                       "ci_state": "passing", "failing_checks": []}},
        reviews={42: [{"id": "rc-1", "state": "CHANGES_REQUESTED",
                      "body": "please rename this variable", "author": "reviewer1"}]},
    )
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.IN_REVIEW)
    disp.sync_vcs(cfg, now=1000)

    main(["--home", str(cfg.home), "events", "T-5"])
    events = json.loads(capsys.readouterr().out)
    review_evs = [e for e in events if e["type"] == "ReviewFeedbackReceived"]
    assert len(review_evs) == 1
    assert review_evs[0]["payload"]["body"] == "please rename this variable"
    assert review_evs[0]["payload"]["comment_id"] == "rc-1"

    phase_evs = [e for e in events if e["type"] == "PhaseChanged"
                and e["payload"].get("phase") == Phase.IMPLEMENTING.value]
    assert phase_evs and "please rename this variable" in phase_evs[-1]["payload"]["reason"]


def test_review_feedback_idempotent_per_comment_id(cfg, monkeypatch):
    fake = FakeVCS(
        statuses={42: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha5",
                       "ci_state": "passing", "failing_checks": []}},
        reviews={42: [{"id": "rc-1", "state": "APPROVED", "body": "lgtm", "author": "r1"}]},
    )
    _use_fake(cfg, monkeypatch, fake, interval=0)
    _seed(cfg, "T-5", Phase.IN_REVIEW)

    disp.sync_vcs(cfg, now=1000)
    disp.sync_vcs(cfg, now=2000)  # same comment id re-observed -> idempotent no-op

    evs = event_log.read(cfg.home, "T-5")
    review_evs = [e for e in evs if e["type"] == "ReviewFeedbackReceived"]
    assert len(review_evs) == 1


def test_in_review_is_a_sleeping_phase():
    assert Phase.IN_REVIEW in SLEEPING_PHASES


def test_dispatch_sweep_spawns_zero_reconcilers_for_in_review_ticket(cfg):
    """With IN_REVIEW in SLEEPING_PHASES, a real dispatch() sweep over a ticket
    sitting in in-review (no requeue timer pending) spawns no reconciler at all —
    `sync_vcs` (or nothing, if vcs="none") owns advancing it, not a spawned agent."""
    _seed(cfg, "T-5", Phase.IN_REVIEW)
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.spawned == []
    assert report.due == []
