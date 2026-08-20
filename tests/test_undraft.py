"""T-86/T-102: dispatcher-owned PR undrafting. `sync_vcs`'s per-poll
`pr_status` now observes `isDraft` (folded into `snap.pr_draft` on every
poll, see `test_pr_status_observes_draft_...` below) and, once CI is
passing, the ticket is still in a reviewable phase, and every current-hash AC
carries a passing spec-axis QA verdict, issues `gh pr ready` exactly once and
appends `PrUpdated {"draft": false}`. A fresh `CHANGES_REQUESTED` review does
NOT gate this independently: `_observe_reviews` runs first in the same tick
and routes it to `implementing`, whose `PhaseChanged` blocks undraft via the
phase guard, not the `unresolved_reviews` conjunct (which only ever fires on
a lost fencing CAS -- see `test_undraft_blocked_by_lost_cas_review_remnant`
below). A failed `gh pr ready` never wedges the ticket; a second sweep after
success is a true no-op.
"""
from maestro import dispatcher as disp
from maestro import event_log, ops, providers, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


class FakeVCS:
    """The only mock: the external GitHub boundary. `pr_ready` records every
    call and, on success, flips its own `statuses[pr]["draft"]` to False --
    standing in for GitHub's real state actually changing, so a later poll in
    the same test sees the undrafted PR exactly like the real API would."""

    def __init__(self, status, reviews=None, ready_fail_first=False):
        self.status = dict(status)
        self.reviews = reviews or []
        self.status_calls = 0
        self.ready_calls: list[tuple] = []
        self._ready_fail_first = ready_fail_first

    def pr_for_branch(self, branch, repo=None, env=None):
        return None

    def pr_status(self, pr_number, repo=None, env=None):
        self.status_calls += 1
        return dict(self.status)

    def review_feedback(self, pr_number, repo=None, env=None):
        return self.reviews

    def pr_ready(self, pr_number, repo=None, env=None):
        self.ready_calls.append((repo, pr_number))
        if self._ready_fail_first and len(self.ready_calls) == 1:
            return {"ok": False, "error": "unknown"}
        self.status["draft"] = False
        return {"ok": True}


def _seed(cfg, key, phase, *, pr=42, draft=True, qa_pass=True):
    store.atomic_write(
        store.spec_path(cfg.home, key),
        f"# {key}\n\n## Acceptance criteria\n- [ ] the widget works\n",
    )
    spec_hash = disp.spec_hash_on_disk(cfg.home, key)
    event_log.append(cfg.home, key, "TicketCreated", {"title": key, "spec_hash": spec_hash},
                     actor="d")
    event_log.append(cfg.home, key, "PrOpened",
                     {"number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": draft},
                     actor="r")
    if qa_pass is not None:
        # T-85: record_qa_verdict now refuses outside the qa phase, so seed the
        # verdict there before landing on the target phase below.
        event_log.append(cfg.home, key, "PhaseChanged", {"phase": Phase.QA.value}, actor="r")
        snap_mod.rebuild(cfg.home, key)
        ops.record_qa_verdict(cfg, key, 1, "pass" if qa_pass else "fail", "looks right")
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(cfg.home, key)


def _use_fake(cfg, monkeypatch, fake, *, interval=0):
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": interval}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)


PASSING = {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
          "ci_state": "passing", "failing_checks": [], "draft": True}


def test_undraft_fires_once_when_all_three_gates_pass(cfg, monkeypatch):
    fake = FakeVCS(PASSING)
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.IN_REVIEW)

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert fake.ready_calls == [("x/y", 42)]
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.pr_draft is False
    events = [e["type"] for e in event_log.read(cfg.home, "T-5")]
    assert events.count("PrUpdated") == 1


def test_undraft_gated_by_ci_not_passing(cfg, monkeypatch):
    status = dict(PASSING, ci_state="pending")
    fake = FakeVCS(status)
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.IN_REVIEW)

    disp.sync_vcs(cfg, now=1000)

    assert fake.ready_calls == []
    assert snap_mod.load(cfg.home, "T-5").pr_draft is True


def test_undraft_gated_by_ac_lacking_passing_spec_qa_verdict(cfg, monkeypatch):
    fake = FakeVCS(PASSING)
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.IN_REVIEW, qa_pass=None)  # never independently QA'd

    disp.sync_vcs(cfg, now=1000)

    assert fake.ready_calls == []
    assert snap_mod.load(cfg.home, "T-5").pr_draft is True


def test_fresh_changes_requested_routes_to_implementing_before_undraft_runs(cfg, monkeypatch):
    """T-102: a fresh CHANGES_REQUESTED is NOT what blocks undraft here -- it
    never reaches the `unresolved_reviews` conjunct at all. `_observe_reviews`
    runs first in the same tick, appends the review, and routes the ticket to
    `implementing`; `_maybe_undraft`'s own phase guard (AWAITING_CI/IN_REVIEW
    only) is what then blocks `pr_ready`, on a phase that is no longer either
    of those. Proof: line-tracing the real `sync_vcs` over this exact scenario
    shows the phase guard return taken and the review conjunct never
    evaluated (see T-102's spec `Notes`)."""
    fake = FakeVCS(PASSING, reviews=[{"id": "rc-1", "state": "CHANGES_REQUESTED",
                                      "body": "nit", "author": "reviewer1"}])
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.IN_REVIEW)

    disp.sync_vcs(cfg, now=1000)

    assert fake.ready_calls == []
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.pr_draft is True
    assert snap.phase == Phase.IMPLEMENTING.value  # the review routing is what actually blocked this
    assert snap.unresolved_reviews == 0  # PhaseChanged already zeroed it -- the conjunct never saw it set


def test_undraft_blocked_by_lost_cas_review_remnant(cfg, monkeypatch):
    """T-102: the ONE reachable path to the `unresolved_reviews` conjunct --
    a lost fencing CAS in `_observe_reviews`' `set_phase` call leaves a
    ReviewFeedbackReceived event on the log with no following PhaseChanged,
    so the ticket is still AWAITING_CI/IN_REVIEW on the next sweep with the
    counter set. Seed that remnant directly on the event log (bypassing
    `_observe_reviews`, which we can't make race deterministically) and
    assert a real `sync_vcs` sweep still refuses to undraft -- this test
    fails if the conjunct is ever deleted."""
    fake = FakeVCS(PASSING)  # review_feedback() empty -- _observe_reviews itself is a no-op this sweep
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.IN_REVIEW)
    event_log.append(cfg.home, "T-5", "ReviewFeedbackReceived",
                     {"comment_id": "rc-1", "state": "CHANGES_REQUESTED", "body": "nit",
                      "author": "reviewer1"},
                     actor="dispatcher", step_id="review-T-5-rc-1")
    snap = snap_mod.rebuild(cfg.home, "T-5")
    assert snap.unresolved_reviews == 1
    assert snap.phase == Phase.IN_REVIEW.value

    disp.sync_vcs(cfg, now=1000)

    assert fake.ready_calls == []
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.pr_draft is True
    assert snap.unresolved_reviews == 1  # still stuck -- exactly the "unroutable" remnant the docstring names


def test_undraft_round_trip_ignores_stale_undismissed_review(cfg, monkeypatch):
    """T-102: pins the deliberate round-trip behaviour so a future change that
    tries to block on ANY undismissed review (not just a lost-CAS remnant)
    fails loudly. `review_feedback()` returns the same undismissed
    CHANGES_REQUESTED across two real `sync_vcs` sweeps; between them the
    ticket is fixed and re-QA'd through implementing -> qa -> awaiting-ci via
    real ops verbs (a fresh review comment id would also work, but reusing
    `rc-1` is what proves GitHub's own non-dismissal, not a dedupe artifact,
    is the reason this doesn't re-block). The second sweep -- CI passing,
    phase back in a reviewable state, QA re-passed -- DOES undraft."""
    reviews = [{"id": "rc-1", "state": "CHANGES_REQUESTED", "body": "nit",
               "author": "reviewer1"}]
    fake = FakeVCS(PASSING, reviews=reviews)
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.IN_REVIEW)

    disp.sync_vcs(cfg, now=1000)  # sweep 1: routes to implementing, does not undraft
    snap = snap_mod.load(cfg.home, "T-5")
    assert fake.ready_calls == []
    assert snap.phase == Phase.IMPLEMENTING.value

    # Fix + re-QA through real ops verbs, same as the implementer/qa phases would.
    ops.verify_ac(cfg, "T-5", 1, {"what": "fixed", "where": "test_undraft.py", "result": "ok"})
    snap = snap_mod.rebuild(cfg.home, "T-5")
    ops.set_phase(cfg, "T-5", Phase.QA, reason="fixed", expect=snap.observed_seq)
    snap = snap_mod.rebuild(cfg.home, "T-5")
    ops.record_qa_verdict(cfg, "T-5", 1, "pass", "fixed and re-verified")
    ops.set_phase(cfg, "T-5", Phase.AWAITING_CI, reason="re-qa'd", expect=snap.observed_seq + 1)

    disp.sync_vcs(cfg, now=2000)  # sweep 2: same undismissed rc-1 review, but this time it undrafts

    assert fake.ready_calls == [("x/y", 42)]
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.pr_draft is False


def test_undraft_idempotent_on_a_second_sweep(cfg, monkeypatch):
    fake = FakeVCS(PASSING)
    _use_fake(cfg, monkeypatch, fake, interval=0)
    _seed(cfg, "T-5", Phase.IN_REVIEW)

    disp.sync_vcs(cfg, now=1000)
    assert fake.ready_calls == [("x/y", 42)]

    disp.sync_vcs(cfg, now=2000)  # fake now reports draft=False, like real GitHub would
    assert fake.ready_calls == [("x/y", 42)]  # no second call
    events = [e["type"] for e in event_log.read(cfg.home, "T-5")]
    assert events.count("PrUpdated") == 1


def test_undraft_ships_dark_for_vcs_none(cfg):
    _seed(cfg, "T-5", Phase.IN_REVIEW)
    before = list(event_log.read(cfg.home, "T-5"))

    result = disp.sync_vcs(cfg, now=1000)

    assert result == {"checked": 0}
    assert list(event_log.read(cfg.home, "T-5")) == before


def test_undraft_ships_dark_for_mode_local(home, monkeypatch):
    """A `mode: local` binding never opens a PR at all (AD-6: `finalize` goes
    straight to `done`, no branch/worktree/PR) -- so `sync_vcs`'s per-key loop
    never even considers it (no `pr_number`), and this T-86 change introduces
    no new code path for it either."""
    target = home.parent / "vault"
    target.mkdir()
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nbranch_prefix = "maestro/"\n\n'
        f'[repos.vault]\npath = "{target}"\nmode = "local"\n',
        encoding="utf-8",
    )
    from maestro import config as config_mod
    cfg = config_mod.load(str(home))
    store.atomic_write(store.spec_path(home, "V-1"),
                       "# V-1\nrepo: vault\n\n## Acceptance criteria\n- [ ] x\n")
    event_log.append(home, "V-1", "TicketCreated", {"title": "V-1", "repo": "vault"}, actor="d")
    ops.set_phase(cfg, "V-1", Phase.READY, reason="auto")
    ops.set_phase(cfg, "V-1", Phase.IMPLEMENTING, reason="local target ready")
    ops.verify_ac(cfg, "V-1", 1, {"what": "read it back", "where": "x", "result": "ok"})
    ops.finalize(cfg, "V-1")

    fake = FakeVCS(PASSING)
    _use_fake(cfg, monkeypatch, fake)
    before = list(event_log.read(home, "V-1"))

    result = disp.sync_vcs(cfg, now=1000)

    assert result["checked"] == 0
    assert fake.ready_calls == []
    assert list(event_log.read(home, "V-1")) == before


def test_failed_gh_pr_ready_is_recorded_and_retried_next_sweep(cfg, monkeypatch):
    fake = FakeVCS(PASSING, ready_fail_first=True)
    _use_fake(cfg, monkeypatch, fake, interval=0)
    _seed(cfg, "T-5", Phase.IN_REVIEW)

    disp.sync_vcs(cfg, now=1000)
    assert len(fake.ready_calls) == 1
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.pr_draft is True  # still draft -- the failure never wedges the ticket
    assert snap.phase == Phase.IN_REVIEW.value
    notes = [e for e in event_log.read(cfg.home, "T-5") if e["type"] == "Note"]
    assert any("gh pr ready failed" in n["payload"].get("text", "") for n in notes)

    disp.sync_vcs(cfg, now=2000)  # retried -- and this time it succeeds
    assert len(fake.ready_calls) == 2
    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.pr_draft is False


def test_pr_status_observes_draft_and_refreshes_the_snapshot_every_poll(cfg, monkeypatch):
    """AC1: the observation is unconditional -- even with CI not passing (so
    the undraft gate itself never fires), a freshly polled `isDraft` still
    folds into `snap.pr_draft` instead of staying frozen at the implementer's
    original `PrOpened` append."""
    status = dict(PASSING, ci_state="failing", failing_checks=["build"], draft=False)
    fake = FakeVCS(status)
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.IN_REVIEW, draft=True)

    disp.sync_vcs(cfg, now=1000)

    assert fake.ready_calls == []  # CI failing -- the undraft gate never fires
    assert snap_mod.load(cfg.home, "T-5").pr_draft is False  # but the observation still folded
