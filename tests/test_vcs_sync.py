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
    """The only mock: the external GitHub boundary (the VCS Protocol seam).

    ``statuses``/``reviews`` may be keyed by a bare ``pr_number`` (single-repo
    boards, byte-identical to before) or by ``(repo, pr_number)`` (multi-repo
    tests) — ``_lookup`` tries the repo-qualified key first so both styles can
    share one fake. ``status_calls``/``review_calls`` record ``(repo, pr_number)``
    tuples so tests can assert exactly which repo each call targeted.
    """

    def __init__(self, statuses=None, reviews=None):
        self.statuses = statuses or {}
        self.reviews = reviews or {}
        self.status_calls: list[tuple] = []
        self.review_calls: list[tuple] = []

    @staticmethod
    def _lookup(table, repo, pr_number, default):
        if (repo, pr_number) in table:
            return table[(repo, pr_number)]
        return table.get(pr_number, default)

    def pr_for_branch(self, branch, repo=None):
        return None

    def pr_status(self, pr_number: int, repo: str | None = None) -> dict:
        self.status_calls.append((repo, pr_number))
        return self._lookup(self.statuses, repo, pr_number, {
            "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
            "ci_state": "unknown", "failing_checks": [],
        })

    def review_feedback(self, pr_number: int, repo: str | None = None) -> list[dict]:
        self.review_calls.append((repo, pr_number))
        return self._lookup(self.reviews, repo, pr_number, [])


def _seed(cfg, key, phase, pr=42, repo=None, pr_url=None):
    store.atomic_write(store.spec_path(cfg.home, key), f"# {key}\napproval_tier: 0\n")
    spec_hash = disp.spec_hash_on_disk(cfg.home, key)
    created = {"title": key, "spec_hash": spec_hash}
    if repo:
        created["repo"] = repo
    event_log.append(cfg.home, key, "TicketCreated", created, actor="d")
    event_log.append(cfg.home, key, "PrOpened",
                     {"number": pr, "url": pr_url or f"https://github.com/x/y/pull/{pr}",
                      "draft": False}, actor="r")
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

    # _seed's PrOpened.url is "https://github.com/x/y/pull/42" — with no snapshot.repo
    # binding, resolve_vcs_slug falls back to the pr_url-parse shim, so the slug
    # "x/y" is threaded through even on this single-repo board.
    disp.sync_vcs(cfg, now=1000)
    assert fake.status_calls == [("x/y", 42)]
    disp.sync_vcs(cfg, now=1100)  # within the 900s window -> no re-poll
    assert fake.status_calls == [("x/y", 42)]
    disp.sync_vcs(cfg, now=2000)  # past the window -> polls again
    assert fake.status_calls == [("x/y", 42), ("x/y", 42)]


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


# --- GA-6: classify gh failures instead of flattening them to ci_state=unknown --
# `FakeVCS` statuses may now carry an "error" key ("auth"|"not_found"|"transient"
# |"unknown") the way a real `GitHubCliVCS.pr_status` failure would -- exactly
# the new field this ticket adds (`.get`-read, so every OTHER FakeVCS status
# above, with no "error" key at all, keeps behaving exactly as before).

def test_sync_vcs_transient_error_is_a_true_no_op(cfg, monkeypatch):
    """A transient gh failure (timeout/network blip) must not spend the failure
    budget, change phase, or clobber an already-known ci_state -- it changes
    NOTHING, so the next poll is a completely free retry."""
    fake = FakeVCS(statuses={42: {
        "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
        "ci_state": "passing", "failing_checks": [],
    }})
    _use_fake(cfg, monkeypatch, fake, interval=0)
    _seed(cfg, "T-5", Phase.AWAITING_CI)
    disp.sync_vcs(cfg, now=1000)  # establishes a known-good ci_state=passing
    before = snap_mod.load(cfg.home, "T-5").to_dict()
    assert before["ci_state"] == "passing"
    assert before["phase"] == Phase.IN_REVIEW.value

    fake.statuses[42] = {
        "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
        "ci_state": "unknown", "failing_checks": [], "error": "transient",
    }
    disp.sync_vcs(cfg, now=2000)

    after = snap_mod.load(cfg.home, "T-5").to_dict()
    assert after == before  # completely unchanged, not just ci_state
    assert not [e for e in event_log.read(cfg.home, "T-5") if e["type"] == "CiObserved"][1:]
    assert not [e for e in event_log.read(cfg.home, "T-5") if e["type"] == "Failed"]


def test_sync_vcs_unknown_classified_error_reproduces_todays_behavior(cfg, monkeypatch):
    """An `error: "unknown"` result (real GitHubCliVCS's genuinely-unrecognized-
    stderr case) must reproduce today's behavior exactly: a plain CiObserved
    with state "unknown" and no routing -- same as the pre-existing FakeVCS
    default (ci_state="unknown", no "error" key at all) other tests rely on."""
    fake = FakeVCS(statuses={42: {
        "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
        "ci_state": "unknown", "failing_checks": [], "error": "unknown",
    }})
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)

    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.AWAITING_CI.value  # no routing
    assert snap.ci_state == "unknown"
    evs = event_log.read(cfg.home, "T-5")
    ci = [e for e in evs if e["type"] == "CiObserved"]
    assert len(ci) == 1
    assert ci[0]["payload"]["state"] == "unknown"
    assert not [e for e in evs if e["type"] == "Failed"]


def test_sync_vcs_auth_failure_routes_to_visible_failure_naming_class_repo_and_pr(cfg, monkeypatch):
    fake = FakeVCS(statuses={42: {
        "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": None,
        "ci_state": "unknown", "failing_checks": [], "error": "auth",
    }})
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.AWAITING_CI)  # _seed's pr_url is https://github.com/x/y/pull/42

    disp.sync_vcs(cfg, now=1000)

    evs = event_log.read(cfg.home, "T-5")
    failed = [e for e in evs if e["type"] == "Failed"]
    assert len(failed) == 1
    error_text = failed[0]["payload"]["error"]
    assert "auth" in error_text and "x/y" in error_text and "42" in error_text

    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.failure_count == 1
    assert snap.last_error and "auth" in snap.last_error

    # `maestro show` surfaces the failure without reading the raw log.
    out = main(["--home", str(cfg.home), "show", "T-5"])
    assert out == 0


def test_sync_vcs_not_found_failure_routes_to_visible_failure(cfg, monkeypatch):
    fake = FakeVCS(statuses={42: {
        "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": None,
        "ci_state": "unknown", "failing_checks": [], "error": "not_found",
    }})
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)

    evs = event_log.read(cfg.home, "T-5")
    failed = [e for e in evs if e["type"] == "Failed"]
    assert len(failed) == 1
    assert "not_found" in failed[0]["payload"]["error"]


def test_sync_vcs_auth_failure_dedupes_ops_fail_across_repeated_polls(cfg, monkeypatch):
    """Repeats of the SAME error still dedupe by construction (same check-key ->
    same step-id): exactly one CiObserved and one `ops.fail` across two ticks."""
    fake = FakeVCS(statuses={42: {
        "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": None,
        "ci_state": "unknown", "failing_checks": [], "error": "auth",
    }})
    _use_fake(cfg, monkeypatch, fake, interval=0)
    _seed(cfg, "T-5", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)
    disp.sync_vcs(cfg, now=2000)  # same auth failure re-observed

    evs = event_log.read(cfg.home, "T-5")
    assert len([e for e in evs if e["type"] == "CiObserved"]) == 1
    assert len([e for e in evs if e["type"] == "Failed"]) == 1


def test_sync_vcs_auth_failure_routes_even_with_preexisting_bare_unknown_event(cfg, monkeypatch):
    """A ticket already carrying a legacy CiObserved{state:"unknown"} (minted by
    the OLD step-id formula, before this ticket's classification existed) must
    still route on the next poll -- the error class changes the check-key so a
    genuine auth/not_found result is never deduped against that stale event."""
    from maestro.idempotency import content_hash

    fake = FakeVCS(statuses={42: {
        "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": None,
        "ci_state": "unknown", "failing_checks": [], "error": "auth",
    }})
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5", Phase.AWAITING_CI)
    legacy_check_key = content_hash("unknown" + ":" + "")
    event_log.append(cfg.home, "T-5", "CiObserved",
                     {"state": "unknown", "failing_checks": [], "detail": ""},
                     actor="dispatcher", step_id=f"ci-T-5-unknown-{legacy_check_key}")

    disp.sync_vcs(cfg, now=1000)

    evs = event_log.read(cfg.home, "T-5")
    ci = [e for e in evs if e["type"] == "CiObserved"]
    assert len(ci) == 2  # the legacy bare one, plus this ticket's freshly-keyed one
    assert [e for e in evs if e["type"] == "Failed"]


# --- GA-6 AC9: the REAL GitHubCliVCS end-to-end through a full dispatch() sweep,
# with `_run` (the actual `gh` subprocess boundary) as the ONLY mock -- GitHubCliVCS,
# _observe_ci and ops all run for real. Everything above this uses FakeVCS (a mock
# of GitHubCliVCS itself, one layer higher); these four prove the real classifier
# integration wires all the way through pr_status -> _observe_ci -> dispatch().

def _use_real_github_cli_vcs(cfg, monkeypatch, view_response):
    """Configures the real `github_cli` vcs provider and monkeypatches only
    `maestro.providers.cli._run` -- the gh subprocess boundary -- so the
    resulting GitHubCliVCS instance is the genuine article. `view_response` is
    the (rc, stdout, stderr) `_run` would have returned for `gh pr view ...
    --json state,mergeable,headRefOid,statusCheckRollup`; the `--json reviews`
    call (from `_observe_reviews`) is stubbed to a harmless empty result so
    only the CI-observation path under test is exercised."""
    from maestro.providers import cli as cli_mod

    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": 0}}}

    def fake_run(cmd, timeout=60):
        if "reviews" in cmd:
            return 0, '{"reviews": []}', ""
        return view_response

    monkeypatch.setattr(cli_mod, "_run", fake_run)


def test_real_githubclivcs_auth_failure_through_full_dispatch_sweep(cfg, monkeypatch):
    _use_real_github_cli_vcs(cfg, monkeypatch, (
        1, "", "HTTP 401: Bad credentials (https://api.github.com/graphql)\n"
               "Try authenticating with:  gh auth login -h github.com"))
    _seed(cfg, "T-9", Phase.AWAITING_CI)

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    evs = event_log.read(cfg.home, "T-9")
    ci = [e for e in evs if e["type"] == "CiObserved"]
    assert len(ci) == 1 and ci[0]["payload"]["state"] == "unknown"
    assert ci[0]["payload"]["error"] == "auth"
    failed = [e for e in evs if e["type"] == "Failed"]
    assert len(failed) == 1 and "auth" in failed[0]["payload"]["error"]

    snap = snap_mod.load(cfg.home, "T-9")
    assert snap.failure_count == 1
    assert main(["--home", str(cfg.home), "show", "T-9"]) == 0


def test_real_githubclivcs_not_found_failure_through_full_dispatch_sweep(cfg, monkeypatch):
    _use_real_github_cli_vcs(cfg, monkeypatch, (
        1, "", "GraphQL: Could not resolve to a Repository with the name "
               "'owner/repo'. (repository)"))
    _seed(cfg, "T-9", Phase.AWAITING_CI)

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    evs = event_log.read(cfg.home, "T-9")
    ci = [e for e in evs if e["type"] == "CiObserved"]
    assert len(ci) == 1 and ci[0]["payload"]["error"] == "not_found"
    failed = [e for e in evs if e["type"] == "Failed"]
    assert len(failed) == 1 and "not_found" in failed[0]["payload"]["error"]

    snap = snap_mod.load(cfg.home, "T-9")
    assert snap.failure_count == 1
    assert main(["--home", str(cfg.home), "show", "T-9"]) == 0


def test_real_githubclivcs_transient_failure_through_full_dispatch_sweep(cfg, monkeypatch):
    # The exact (rc, stderr) shape `_run` itself produces on a real
    # subprocess.TimeoutExpired (see test_run_timeout_expired_is_transient).
    _use_real_github_cli_vcs(cfg, monkeypatch, (
        124, "", "maestro: command timed out after 60s: Command '[\'gh\']' "
                 "timed out after 60 seconds"))
    _seed(cfg, "T-9", Phase.AWAITING_CI)

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    evs = event_log.read(cfg.home, "T-9")
    assert not [e for e in evs if e["type"] == "CiObserved"]
    assert not [e for e in evs if e["type"] == "Failed"]
    snap = snap_mod.load(cfg.home, "T-9")
    assert snap.phase == Phase.AWAITING_CI.value
    assert snap.failure_count == 0


def test_real_githubclivcs_unknown_failure_through_full_dispatch_sweep(cfg, monkeypatch):
    _use_real_github_cli_vcs(cfg, monkeypatch,
        (1, "", "gh: some brand new error format this classifier has never seen"))
    _seed(cfg, "T-9", Phase.AWAITING_CI)

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    evs = event_log.read(cfg.home, "T-9")
    ci = [e for e in evs if e["type"] == "CiObserved"]
    assert len(ci) == 1 and ci[0]["payload"]["state"] == "unknown"
    assert ci[0]["payload"]["error"] == "unknown"
    assert not [e for e in evs if e["type"] == "Failed"]
    snap = snap_mod.load(cfg.home, "T-9")
    assert snap.phase == Phase.AWAITING_CI.value
    assert snap.failure_count == 0


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


# --- MR-4: repo-scoped VCS — each ticket's PR is read from its OWN repo ---------

def _two_repo_cfg(cfg):
    cfg.repos = {
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha",
                  "base_branch": "main", "branch_prefix": "maestro/"},
        "beta": {"path": "/repo/beta", "slug": "acme/beta",
                 "base_branch": "main", "branch_prefix": "maestro/"},
    }
    return cfg


def test_two_repo_sweep_reads_each_tickets_own_repo_and_isolates_merge(cfg, monkeypatch):
    """Pinned regression: before repo-scoping, sync_vcs always read repos[0]'s PR
    status for every awaiting-ci/in-review ticket regardless of which repo it
    actually lived in — with two repos sharing PR number 7, ticket A (repo alpha)
    could be wrongly finalized by ticket B's (repo beta) merge. Now each ticket's
    own repo is polled, so only B (whose PR is actually merged) finalizes."""
    _two_repo_cfg(cfg)
    fake = FakeVCS(statuses={
        ("acme/alpha", 7): {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha-a",
                            "ci_state": "passing", "failing_checks": []},
        ("acme/beta", 7): {"state": "MERGED", "mergeable": "UNKNOWN", "head_sha": "sha-b",
                           "ci_state": "passing", "failing_checks": []},
    })
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "A", Phase.IN_REVIEW, pr=7, repo="alpha",
         pr_url="https://github.com/acme/alpha/pull/7")
    _seed(cfg, "B", Phase.IN_REVIEW, pr=7, repo="beta",
         pr_url="https://github.com/acme/beta/pull/7")

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert ("acme/alpha", 7) in fake.status_calls
    assert ("acme/beta", 7) in fake.status_calls

    snap_a = snap_mod.load(cfg.home, "A")
    snap_b = snap_mod.load(cfg.home, "B")
    assert snap_b.phase == Phase.DONE.value
    # The pinned regression: A must NOT be finalized by B's merge.
    assert snap_a.phase == Phase.IN_REVIEW.value

    evs_a = event_log.read(cfg.home, "A")
    evs_b = event_log.read(cfg.home, "B")
    assert not [e for e in evs_a if e["type"] in ("PrUpdated", "Finalized")]
    assert [e for e in evs_b if e["type"] == "PrUpdated" and e["payload"].get("merged")]
    assert [e for e in evs_b if e["type"] == "Finalized"]


def test_legacy_ticket_slug_resolved_from_pr_url_shim(cfg, monkeypatch):
    """A pre-binding ticket (snapshot.repo unset) still gets its PR's own repo
    threaded through, via the pr_url-parse shim in repos.py — the only per-ticket
    artifact that already identifies the repo for tickets minted before repo
    binding existed."""
    fake = FakeVCS(statuses={("acme/beta", 9): {
        "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha9",
        "ci_state": "passing", "failing_checks": [],
    }})
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "LEGACY", Phase.IN_REVIEW, pr=9,
         pr_url="https://github.com/acme/beta/pull/9")  # repo NOT bound

    disp.sync_vcs(cfg, now=1000)
    assert fake.status_calls == [("acme/beta", 9)]
    assert fake.review_calls == [("acme/beta", 9)]


def test_observe_reviews_lands_on_correct_ticket_for_same_numbered_prs_in_two_repos(cfg, monkeypatch):
    _two_repo_cfg(cfg)
    fake = FakeVCS(
        statuses={
            ("acme/alpha", 7): {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha-a",
                                "ci_state": "passing", "failing_checks": []},
            ("acme/beta", 7): {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha-b",
                               "ci_state": "passing", "failing_checks": []},
        },
        reviews={
            ("acme/alpha", 7): [{"id": "a-1", "state": "APPROVED",
                                 "body": "lgtm alpha", "author": "r1"}],
            ("acme/beta", 7): [{"id": "b-1", "state": "APPROVED",
                                "body": "lgtm beta", "author": "r2"}],
        },
    )
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "A", Phase.IN_REVIEW, pr=7, repo="alpha",
         pr_url="https://github.com/acme/alpha/pull/7")
    _seed(cfg, "B", Phase.IN_REVIEW, pr=7, repo="beta",
         pr_url="https://github.com/acme/beta/pull/7")

    disp.sync_vcs(cfg, now=1000)

    evs_a = [e for e in event_log.read(cfg.home, "A") if e["type"] == "ReviewFeedbackReceived"]
    evs_b = [e for e in event_log.read(cfg.home, "B") if e["type"] == "ReviewFeedbackReceived"]
    assert len(evs_a) == 1 and evs_a[0]["payload"]["comment_id"] == "a-1"
    assert len(evs_b) == 1 and evs_b[0]["payload"]["comment_id"] == "b-1"
