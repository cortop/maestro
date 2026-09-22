"""T-123: opt-in CI auto-rerun-once-per-head (dispatcher._observe_ci's failing
branch). With ``ci_auto_rerun`` unset, a failing `pr_status` poll behaves
byte-identically to before this ticket. With it enabled, the FIRST failing
observation for a head SHA requests one `gh run rerun --failed` (via the VCS's
new `rerun_failed`) instead of routing to `implementing`; a later failing
observation for the SAME head SHA, past `ci_rerun_grace` seconds, produces a
fresh `CiObserved` (the existing content-only step-id would otherwise dedupe
it against the pre-rerun one) and routes exactly once. `ci_failure_excerpt`
additionally captures a bounded tail of the failed job's log (via the VCS's
new `failed_log_tail`) into that routing observation.
"""
from maestro import context, dispatcher as disp
from maestro import event_log, providers, snapshot as snap_mod, store
from maestro.statemachine import Phase


class FakeVCS:
    """The only mock: the external GitHub boundary (the VCS Protocol seam),
    extended with T-123's two new methods."""

    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.status_calls: list[tuple] = []
        self.review_calls: list[tuple] = []
        self.rerun_calls: list[tuple] = []
        self.rerun_results: dict[str, dict] = {}
        self.log_tails: dict[str, str] = {}

    def pr_for_branch(self, branch, repo=None, env=None):
        return None

    def pr_status(self, pr_number: int, repo: str | None = None,
                  env: dict | None = None) -> dict:
        self.status_calls.append((repo, pr_number))
        return self.statuses.get(pr_number, {
            "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
            "ci_state": "unknown", "failing_checks": [],
        })

    def review_feedback(self, pr_number: int, repo: str | None = None,
                        env: dict | None = None) -> list[dict]:
        self.review_calls.append((repo, pr_number))
        return []

    def rerun_failed(self, pr_number: int, head_sha: str, repo: str | None = None,
                     env: dict | None = None) -> dict:
        self.rerun_calls.append((pr_number, head_sha))
        return self.rerun_results.get(head_sha, {"ok": True, "run_ids": [f"run-{head_sha}"]})

    def failed_log_tail(self, run_id: str, max_bytes: int = 2048, repo: str | None = None,
                        env: dict | None = None) -> str:
        tail = self.log_tails.get(run_id, "")
        return tail[-max_bytes:] if tail else ""


def _seed(cfg, key, phase, pr=42):
    store.atomic_write(
        store.spec_path(cfg.home, key),
        f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n",
    )
    spec_hash = disp.spec_hash_on_disk(cfg.home, key)
    event_log.append(cfg.home, key, "TicketCreated", {"title": key, "spec_hash": spec_hash},
                     actor="d")
    event_log.append(cfg.home, key, "PrOpened",
                     {"number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": False},
                     actor="r")
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(cfg.home, key)


def _use_fake(cfg, monkeypatch, fake, *, interval=0):
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": interval}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)


def _failing_status(head_sha="sha1", checks=("unit",)):
    return {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": head_sha,
            "ci_state": "failing", "failing_checks": list(checks)}


# --- AC1: ci_auto_rerun unset is byte-identical to before this ticket ---

def test_ci_auto_rerun_unset_is_byte_identical(cfg, monkeypatch):
    fake = FakeVCS({42: _failing_status()})
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-1", Phase.AWAITING_CI)
    assert cfg.ci_auto_rerun is False

    disp.sync_vcs(cfg, now=1000)

    assert fake.rerun_calls == []
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    evs = event_log.read(cfg.home, "T-1")
    ci = [e for e in evs if e["type"] == "CiObserved"]
    assert len(ci) == 1
    assert ci[0]["payload"]["failing_checks"] == ["unit"]
    assert not [e for e in evs if e["type"] == "CiRerunRequested"]
    changed = [e for e in evs if e["type"] == "PhaseChanged"
               and e["payload"].get("phase") == Phase.IMPLEMENTING.value]
    assert changed and "unit" in changed[-1]["payload"]["reason"]


# --- AC2: first failing observation for a head SHA triggers the one rerun,
# withholding routing ---

def test_ci_auto_rerun_first_failing_triggers_rerun_and_withholds_routing(cfg, monkeypatch):
    fake = FakeVCS({42: _failing_status()})
    _use_fake(cfg, monkeypatch, fake)
    cfg.ci_auto_rerun = True
    _seed(cfg, "T-1", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)

    assert fake.rerun_calls == [(42, "sha1")]
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.AWAITING_CI.value  # withheld -- no PhaseChanged
    assert snap.ci_reruns == {"sha1": {"at": 1000, "run_ids": ["run-sha1"]}}
    evs = event_log.read(cfg.home, "T-1")
    assert [e for e in evs if e["type"] == "CiRerunRequested"]
    assert not [e for e in evs if e["type"] == "PhaseChanged"
               and e["payload"].get("phase") == Phase.IMPLEMENTING.value]


def test_ci_auto_rerun_failure_to_resolve_falls_through_to_routing(cfg, monkeypatch):
    """A rerun request the VCS could not actually place (ok=False -- e.g. no
    resolvable run id) must not silently wedge the ticket forever; it routes
    exactly as if ci_auto_rerun were unset for this poll, and records no
    CiRerunRequested (nothing to dedupe a real rerun against later)."""
    fake = FakeVCS({42: _failing_status()})
    fake.rerun_results["sha1"] = {"ok": False}
    _use_fake(cfg, monkeypatch, fake)
    cfg.ci_auto_rerun = True
    _seed(cfg, "T-1", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)

    assert fake.rerun_calls == [(42, "sha1")]
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    assert snap.ci_reruns == {}
    evs = event_log.read(cfg.home, "T-1")
    assert not [e for e in evs if e["type"] == "CiRerunRequested"]


# --- AC3: a failing observation for the same head SHA after the grace window
# appends a fresh CiObserved and routes exactly once; passing after a rerun
# proceeds as today ---

def test_ci_auto_rerun_within_grace_stays_withheld_and_reruns_only_once(cfg, monkeypatch):
    fake = FakeVCS({42: _failing_status()})
    _use_fake(cfg, monkeypatch, fake, interval=0)
    cfg.ci_auto_rerun = True
    _seed(cfg, "T-1", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)   # triggers the rerun
    disp.sync_vcs(cfg, now=1100)   # first in-grace poll -- a fresh (but non-routing) CiObserved,
                                    # since the rerun record just appeared for this head
    disp.sync_vcs(cfg, now=1200)   # second in-grace poll, identical content -- dedupes

    assert fake.rerun_calls == [(42, "sha1")]  # never a second rerun request
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.AWAITING_CI.value
    evs = event_log.read(cfg.home, "T-1")
    assert len([e for e in evs if e["type"] == "CiObserved"]) == 2


def test_ci_auto_rerun_after_grace_routes_with_fresh_ci_observed(cfg, monkeypatch):
    fake = FakeVCS({42: _failing_status()})
    _use_fake(cfg, monkeypatch, fake, interval=0)
    cfg.ci_auto_rerun = True
    cfg.ci_rerun_grace = 900
    _seed(cfg, "T-1", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)  # triggers the one rerun
    # Grace has elapsed and GitHub still reports the SAME failing check (the
    # rerun failed for real) -- the pre-existing content-only step-id would
    # otherwise dedupe this against the pre-rerun CiObserved forever.
    disp.sync_vcs(cfg, now=1000 + 901)

    assert fake.rerun_calls == [(42, "sha1")]  # never a second rerun for this head
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    evs = event_log.read(cfg.home, "T-1")
    ci = [e for e in evs if e["type"] == "CiObserved"]
    assert len(ci) == 2  # the rerun-triggering observation + the post-grace one
    changed = [e for e in evs if e["type"] == "PhaseChanged"
               and e["payload"].get("phase") == Phase.IMPLEMENTING.value]
    assert len(changed) == 1


def test_ci_auto_rerun_changed_failing_set_after_grace_routes_without_second_rerun(cfg, monkeypatch):
    """Q2 (asked during triage): the rerun gate is per-head-SHA, not
    per-failing-set -- a changed failing-check set on an already-rerun SHA
    routes straight to implementing rather than triggering a second rerun."""
    fake = FakeVCS({42: _failing_status(checks=("unit",))})
    _use_fake(cfg, monkeypatch, fake, interval=0)
    cfg.ci_auto_rerun = True
    _seed(cfg, "T-1", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)
    assert fake.rerun_calls == [(42, "sha1")]

    fake.statuses[42]["failing_checks"] = ["lint"]  # a different flaky test, same head
    disp.sync_vcs(cfg, now=1000 + 901)

    assert fake.rerun_calls == [(42, "sha1")]  # gated per head SHA, not per failing-set
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    assert snap.failing_checks == ["lint"]


def test_ci_auto_rerun_passing_after_rerun_routes_to_in_review_as_today(cfg, monkeypatch):
    fake = FakeVCS({42: _failing_status()})
    _use_fake(cfg, monkeypatch, fake, interval=0)
    cfg.ci_auto_rerun = True
    _seed(cfg, "T-1", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)  # triggers rerun, withheld
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_CI.value

    fake.statuses[42] = {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
                         "ci_state": "passing", "failing_checks": []}
    disp.sync_vcs(cfg, now=1050)  # well within grace -- passing is unaffected by the gate

    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.IN_REVIEW.value


# --- AC4: a new head SHA gets its own single rerun ---

def test_ci_auto_rerun_new_head_sha_gets_its_own_rerun(cfg, monkeypatch):
    fake = FakeVCS({42: _failing_status(head_sha="sha1")})
    _use_fake(cfg, monkeypatch, fake, interval=0)
    cfg.ci_auto_rerun = True
    _seed(cfg, "T-1", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)
    assert fake.rerun_calls == [(42, "sha1")]

    fake.statuses[42] = _failing_status(head_sha="sha2")  # a new push landed
    disp.sync_vcs(cfg, now=1001)

    assert fake.rerun_calls == [(42, "sha1"), (42, "sha2")]
    snap = snap_mod.load(cfg.home, "T-1")
    assert set(snap.ci_reruns) == {"sha1", "sha2"}
    assert snap.phase == Phase.AWAITING_CI.value  # sha2's own rerun just as withheld


# --- AC5: ci_failure_excerpt captures a bounded tail into the routing
# CiObserved, surfaced in the implementing reason and derived/context/<KEY>.md ---

def test_ci_failure_excerpt_surfaces_in_reason_and_context(cfg, monkeypatch):
    fake = FakeVCS({42: _failing_status()})
    fake.log_tails["run-sha1"] = "AssertionError: boom\n" * 200  # exercises the 2KB cap
    _use_fake(cfg, monkeypatch, fake, interval=0)
    cfg.ci_auto_rerun = True
    cfg.ci_failure_excerpt = True
    _seed(cfg, "T-1", Phase.AWAITING_CI)

    disp.sync_vcs(cfg, now=1000)  # triggers rerun, no excerpt captured yet
    evs = event_log.read(cfg.home, "T-1")
    assert "failure_excerpt" not in [e for e in evs if e["type"] == "CiObserved"][0]["payload"]

    disp.sync_vcs(cfg, now=1000 + 901)  # past grace, still failing -> routes with excerpt

    evs = event_log.read(cfg.home, "T-1")
    routing_ci = [e for e in evs if e["type"] == "CiObserved"][-1]
    excerpt = routing_ci["payload"]["failure_excerpt"]
    assert excerpt
    assert len(excerpt.encode("utf-8")) <= 2048

    changed = [e for e in evs if e["type"] == "PhaseChanged"
               and e["payload"].get("phase") == Phase.IMPLEMENTING.value]
    assert excerpt in changed[-1]["payload"]["reason"]

    ctx_text = context.context_path(cfg.home, "T-1").read_text(encoding="utf-8")
    assert excerpt.splitlines()[0] in ctx_text


# --- AC6: NullVCS no-ops both new methods ---

def test_null_vcs_rerun_and_log_tail_are_no_ops():
    from maestro.providers.base import NullVCS
    vcs = NullVCS()
    assert vcs.rerun_failed(1, "sha1") == {"ok": False}
    assert vcs.failed_log_tail("run-1") == ""
