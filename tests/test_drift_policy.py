"""MTO-2: `base_drift_policy` decides whether an awaiting-ci/in-review ticket's
worktree merely being BEHIND its base branch (not GitHub-CONFLICTING) alone
routes it back to `implementing` (dispatcher.sync_worktrees -> ops.route_stale).
Unconditional drift-routing livelocks a fast-moving shared integration branch
(every re-route force-pushes, restarting CI, forever) -- see the ticket spec
for the measured incident. "on_conflict" (the new default) never routes on
drift alone; "daily" routes at most once per calendar day per ticket; "always"
is byte-identical to the pre-ticket behavior. Every scenario drives real git
repos and the real `maestro` config loader/dispatcher -- the only accepted
mock is the VCS provider boundary (AC3's CONFLICTING scenario), exactly as
test_worktree_sync_multi_repo.py already does.
"""
import json

import pytest

from maestro import config as config_mod, dispatcher as disp, event_log, providers, \
    snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git as _git, make_origin_and_repo as _make_origin_and_repo


def _add_worktree(repo, home, key, branch, base="main"):
    wt = home / "worktrees" / key
    _git("worktree", "add", "-q", "-b", branch, str(wt), base, cwd=repo)
    return wt


def _seed(home, key, phase, pr=10, *, repo_name=None):
    # T-82: an optional `repo: <name>` frontmatter/payload binding, mirroring
    # test_worktree_sync_multi_repo.py's own _seed -- needed by the per-ticket
    # (not per-group) binding tests below (AC1-AC4).
    front = f"repo: {repo_name}\n" if repo_name else ""
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\napproval_tier: 0\n{front}\n## Acceptance criteria\n- [ ] ok\n")
    payload = {"title": key}
    if repo_name:
        payload["repo"] = repo_name
    event_log.append(home, key, "TicketCreated", payload, actor="d")
    event_log.append(home, key, "PrOpened",
                     {"number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": False},
                     actor="r")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(home, key)


def _merge_commits_to_origin(repo, origin, n=1, base="main"):
    """Simulate n commits landing on origin/<base> (another ticket's PR(s) merging).
    Content includes the current commit count so repeated calls against the
    same repo (as AC9's two-scenario test does) never collide on an
    already-committed, identical NEWS.md body."""
    import subprocess
    for _ in range(n):
        count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo,
                               capture_output=True, text=True, check=True).stdout.strip()
        (repo / "NEWS.md").write_text(f"merged change {count}\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", f"merged change {count}", cwd=repo)
    _git("push", "-q", "origin", base, cwd=repo)


def _ci_passing(home, key):
    """T-82: a drift-only reroute now requires a POSITIVELY known-safe CI state
    (never None/unobserved, never "unknown", see AC5) -- tests exercising the
    always/daily routing mechanism itself (not the CI gate) need an observed
    passing CI up front to still route."""
    event_log.append(home, key, "CiObserved",
                     {"state": "passing", "failing_checks": []}, actor="dispatcher")
    return snap_mod.rebuild(home, key)


def _phase_changed_reasons(home, key):
    return [e["payload"].get("reason", "") for e in event_log.read(home, key)
            if e["type"] == "PhaseChanged" and e["payload"].get("phase") == Phase.IMPLEMENTING.value]


# --- config: accepted values, per-repo override wins, fail-closed on unknown ---

def test_base_drift_policy_default_is_on_conflict(home):
    cfg = config_mod.load(str(home))  # no config.toml at all
    assert cfg.base_drift_policy == "on_conflict"


@pytest.mark.parametrize("policy", ["always", "daily", "on_conflict"])
def test_base_drift_policy_accepted_values_in_maestro_table(home, policy):
    (home / "config.toml").write_text(f'[maestro]\nbase_drift_policy = "{policy}"\n',
                                      encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.base_drift_policy == policy


def test_base_drift_policy_unknown_value_fails_closed_at_maestro_level(home):
    (home / "config.toml").write_text('[maestro]\nbase_drift_policy = "sometimes"\n',
                                      encoding="utf-8")
    with pytest.raises(store.MaestroError) as exc:
        config_mod.load(str(home))
    msg = str(exc.value)
    assert "base_drift_policy" in msg and "sometimes" in msg
    for valid in ("always", "daily", "on_conflict"):
        assert valid in msg  # names the valid set


def test_base_drift_policy_unknown_value_fails_closed_per_repo(home, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (home / "config.toml").write_text(
        f'[repos.x]\npath = "{repo}"\nbase_drift_policy = "sometimes"\n', encoding="utf-8")
    with pytest.raises(store.MaestroError) as exc:
        config_mod.load(str(home))
    msg = str(exc.value)
    assert "[repos.x]" in msg and "base_drift_policy" in msg and "sometimes" in msg


def test_per_repo_base_drift_policy_wins_over_board_wide_default(home, tmp_path):
    from maestro import repos as repos_mod
    repo = tmp_path / "r"
    repo.mkdir()
    (home / "config.toml").write_text(
        f'[maestro]\nbase_drift_policy = "always"\n\n'
        f'[repos.x]\npath = "{repo}"\nbase_drift_policy = "on_conflict"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.base_drift_policy == "always"
    binding = repos_mod._binding_from_table(cfg, "x", cfg.repos["x"])
    assert binding.base_drift_policy == "on_conflict"


def test_per_repo_unset_inherits_board_wide_default(home, tmp_path):
    from maestro import repos as repos_mod
    repo = tmp_path / "r"
    repo.mkdir()
    (home / "config.toml").write_text(
        f'[maestro]\nbase_drift_policy = "daily"\n\n[repos.x]\npath = "{repo}"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    binding = repos_mod._binding_from_table(cfg, "x", cfg.repos["x"])
    assert binding.base_drift_policy == "daily"


# --- AC2: on_conflict skips drift-only routing, but still fast-forwards local base ---

def test_on_conflict_does_not_route_far_behind_ticket_but_still_fast_forwards(home, cfg, tmp_path):
    assert cfg.base_drift_policy == "on_conflict"  # the default -- board "sets nothing"
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-5", Phase.AWAITING_CI)
    _add_worktree(repo, home, "T-5", "maestro/T-5")
    _merge_commits_to_origin(repo, origin, n=50)  # far behind, not just 1 commit

    result = disp.sync_worktrees(cfg)
    assert result["routed"] == []
    assert result["skipped_by_policy"] == ["T-5"]
    assert snap_mod.load(home, "T-5").phase == Phase.AWAITING_CI.value  # untouched

    # the fetch + local base fast-forward still happened -- only routing is gated
    import subprocess
    after = subprocess.run(["git", "rev-parse", "main"], cwd=repo,
                           capture_output=True, text=True, check=True).stdout.strip()
    origin_tip = subprocess.run(["git", "rev-parse", "origin/main"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout.strip()
    assert after == origin_tip


# --- AC3: on_conflict still routes a GitHub-CONFLICTING PR, via route_conflict ---

class _FakeVCS:
    def __init__(self, statuses):
        self.statuses = statuses

    def pr_for_branch(self, branch, env=None):
        return None

    def pr_status(self, pr_number, repo=None, env=None):
        return self.statuses.get(pr_number, {
            "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
            "ci_state": "unknown", "failing_checks": [],
        })

    def review_feedback(self, pr_number, repo=None, env=None):
        return []


def test_on_conflict_still_routes_a_conflicting_pr_via_route_conflict(home, cfg, tmp_path, monkeypatch):
    assert cfg.base_drift_policy == "on_conflict"
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": 0}}}
    fake = _FakeVCS({42: {"state": "OPEN", "mergeable": "CONFLICTING", "head_sha": "sha1",
                          "ci_state": "unknown", "failing_checks": []}})
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)

    _seed(home, "T-9", Phase.AWAITING_CI, pr=42)
    _add_worktree(repo, home, "T-9", "maestro/T-9")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert snap_mod.load(home, "T-9").phase == Phase.IMPLEMENTING.value
    assert "T-9" in report.spawned


# --- AC4: daily routes at most once per calendar day per ticket ---

def test_daily_routes_at_most_once_per_calendar_day(home, cfg, tmp_path):
    cfg.base_drift_policy = "daily"
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-7", Phase.AWAITING_CI)
    _ci_passing(home, "T-7")
    _add_worktree(repo, home, "T-7", "maestro/T-7")
    _merge_commits_to_origin(repo, origin, n=1)

    # First sweep of the day: drifted, not yet drift-rebased today -> routes.
    result1 = disp.sync_worktrees(cfg, now=1000)
    assert result1["routed"] == ["T-7"]
    assert snap_mod.load(home, "T-7").phase == Phase.IMPLEMENTING.value

    # Simulate the reconciler finishing its rebase pass and the PR landing
    # back in awaiting-ci, still (still, in this test) behind -- an
    # arbitrary number of further same-day sweeps must not re-route it.
    event_log.append(home, "T-7", "PhaseChanged", {"phase": Phase.AWAITING_CI.value}, actor="r")
    snap_mod.rebuild(home, "T-7")

    for i, later_now in enumerate((2000, 3000, 43000), start=2):
        result = disp.sync_worktrees(cfg, now=later_now)
        assert result["routed"] == [], f"sweep {i} re-routed within the same day"
        assert result["skipped_by_policy"] == ["T-7"]
        assert snap_mod.load(home, "T-7").phase == Phase.AWAITING_CI.value

    # Next calendar day (>= 86400s later): still awaiting-ci and still behind -> due again.
    result_next_day = disp.sync_worktrees(cfg, now=1000 + 86400)
    assert result_next_day["routed"] == ["T-7"]


# --- AC5: always is byte-identical for a board that sets nothing but that ---

def test_always_routes_unconditionally_same_as_pre_ticket_behavior(home, cfg, tmp_path):
    cfg.base_drift_policy = "always"
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-8", Phase.AWAITING_CI)
    _ci_passing(home, "T-8")
    _add_worktree(repo, home, "T-8", "maestro/T-8")
    _merge_commits_to_origin(repo, origin, n=1)

    result = disp.sync_worktrees(cfg)
    assert result["routed"] == ["T-8"]
    assert result["skipped_by_policy"] == []
    assert snap_mod.load(home, "T-8").phase == Phase.IMPLEMENTING.value


# --- AC6: under every mode, pending CI checks block a drift-only route ---

@pytest.mark.parametrize("policy", ["always", "daily"])
def test_pending_ci_checks_block_drift_route_under_every_mode(home, cfg, tmp_path, policy):
    # on_conflict is covered by test_on_conflict_does_not_route_far_behind_ticket... --
    # it never routes on drift regardless of CI state. This proves the pending-CI
    # gate independently blocks the two modes that WOULD otherwise route.
    cfg.base_drift_policy = policy
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-6", Phase.AWAITING_CI)
    _add_worktree(repo, home, "T-6", "maestro/T-6")
    _merge_commits_to_origin(repo, origin, n=1)
    event_log.append(home, "T-6", "CiObserved",
                     {"state": "pending", "failing_checks": [], "detail": "3 of 5 checks running"},
                     actor="dispatcher")
    snap_mod.rebuild(home, "T-6")
    assert snap_mod.load(home, "T-6").ci_state == "pending"

    result = disp.sync_worktrees(cfg)
    assert result["routed"] == []
    assert result["skipped_by_policy"] == ["T-6"]
    assert snap_mod.load(home, "T-6").phase == Phase.AWAITING_CI.value


# --- AC8: the set_phase reason names the policy that allowed the re-route ---

@pytest.mark.parametrize("policy", ["always", "daily"])
def test_set_phase_reason_names_the_policy(home, cfg, tmp_path, policy):
    cfg.base_drift_policy = policy
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-4", Phase.AWAITING_CI)
    _ci_passing(home, "T-4")
    _add_worktree(repo, home, "T-4", "maestro/T-4")
    _merge_commits_to_origin(repo, origin, n=1)

    disp.sync_worktrees(cfg)
    reasons = _phase_changed_reasons(home, "T-4")
    assert reasons and f"policy={policy}" in reasons[-1]


# --- AC9: a real dispatcher sweep proves on_conflict no-route + always byte-identical ---

def test_dispatch_sweep_proves_on_conflict_no_route_and_always_byte_identical(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path)

    # on_conflict (the default -- a board that sets nothing but this):
    cfg_on_conflict = Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3,
                             repo_path=str(repo))
    assert cfg_on_conflict.base_drift_policy == "on_conflict"
    _seed(home, "T-OC", Phase.AWAITING_CI)
    _add_worktree(repo, home, "T-OC", "maestro/T-OC")
    _merge_commits_to_origin(repo, origin, n=1)

    disp.dispatch(cfg_on_conflict, DryRunSessions(), now=1000)
    assert snap_mod.load(home, "T-OC").phase == Phase.AWAITING_CI.value  # not routed on drift

    # always (explicit opt-in) -- byte-identical to pre-ticket unconditional routing.
    # report1's own sync_worktrees already fast-forwarded repo's local main to
    # origin's tip -- merge one more commit so T-AL's fresh worktree is behind too.
    cfg_always = Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3,
                        repo_path=str(repo), base_drift_policy="always")
    _seed(home, "T-AL", Phase.AWAITING_CI)
    _ci_passing(home, "T-AL")
    _add_worktree(repo, home, "T-AL", "maestro/T-AL")
    _merge_commits_to_origin(repo, origin, n=1)

    report2 = disp.dispatch(cfg_always, DryRunSessions(), now=2000)
    assert snap_mod.load(home, "T-AL").phase == Phase.IMPLEMENTING.value  # routed
    assert "T-AL" in report2.spawned


# ---------------------------------------------------------------------------
# T-82: the drift decision reads the TICKET's own resolved binding, not the
# GROUP's -- a group is keyed on (realpath, base_branch) only, so a per-repo
# override sharing a checkout with the implicit default (or with another
# [repos.*] table) used to silently inherit whichever binding created the
# group first. AC1-AC4 below drive the reported repro (and its mirror, and
# the two-tickets-diverge proof, and the end-to-end sweep) directly, with the
# real config loader -- never the resolver alone (test_per_repo_..._wins_...
# above already covers the resolver in isolation).
# ---------------------------------------------------------------------------

def test_per_ticket_binding_wins_over_groups_first_arrived_binding_suppresses(home, tmp_path):
    """AC1: the exact reported repro (BLOCKING #2), now green. A [repos.x]
    override sharing the SAME checkout+base as the implicit default (a path
    collision -- [maestro] repo_path == [repos.x] path) must have its OWN
    on_conflict policy read for a ticket bound to it, not the group's
    (whichever binding created the (realpath, base) group first -- normally
    the implicit default, here "always")."""
    origin, repo = _make_origin_and_repo(tmp_path)
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{repo}"\nbase_drift_policy = "always"\n\n'
        f'[repos.x]\npath = "{repo}"\nbase_drift_policy = "on_conflict"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))

    _seed(home, "T-82X", Phase.AWAITING_CI, repo_name="x")
    _add_worktree(repo, home, "T-82X", "maestro/T-82X")
    _merge_commits_to_origin(repo, origin, n=1)

    result = disp.sync_worktrees(cfg)
    assert result["routed"] == []
    assert result["skipped_by_policy"] == ["T-82X"]
    assert snap_mod.load(home, "T-82X").phase == Phase.AWAITING_CI.value  # untouched


def test_per_ticket_binding_wins_over_groups_first_arrived_binding_routes(home, tmp_path):
    """AC2: the mirror case -- the per-repo override says "always", the
    board-wide default says "on_conflict". The ticket must route, proving the
    override wins in both directions, not just the suppress direction."""
    origin, repo = _make_origin_and_repo(tmp_path)
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{repo}"\nbase_drift_policy = "on_conflict"\n\n'
        f'[repos.x]\npath = "{repo}"\nbase_drift_policy = "always"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))

    _seed(home, "T-82Y", Phase.AWAITING_CI, repo_name="x")
    _ci_passing(home, "T-82Y")
    _add_worktree(repo, home, "T-82Y", "maestro/T-82Y")
    _merge_commits_to_origin(repo, origin, n=1)

    result = disp.sync_worktrees(cfg)
    assert result["routed"] == ["T-82Y"]
    assert result["skipped_by_policy"] == []
    assert snap_mod.load(home, "T-82Y").phase == Phase.IMPLEMENTING.value


def test_two_tickets_same_checkout_and_base_different_repo_policies_diverge(home, tmp_path):
    """AC3: two tickets on the SAME checkout+base, bound to two different
    [repos.*] tables carrying different policies, get DIFFERENT outcomes from
    the SAME sweep -- proving the decision is genuinely per-ticket, not
    per-group (a per-group read could only ever produce one outcome for
    both, whichever binding happened to create the group first)."""
    origin, repo = _make_origin_and_repo(tmp_path)
    (home / "config.toml").write_text(
        f'[repos.x]\npath = "{repo}"\nbase_drift_policy = "on_conflict"\n\n'
        f'[repos.y]\npath = "{repo}"\nbase_drift_policy = "always"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))

    _seed(home, "T-82A", Phase.AWAITING_CI, repo_name="x")
    _add_worktree(repo, home, "T-82A", "maestro/T-82A")
    _seed(home, "T-82B", Phase.AWAITING_CI, repo_name="y")
    _ci_passing(home, "T-82B")
    _add_worktree(repo, home, "T-82B", "maestro/T-82B")
    _merge_commits_to_origin(repo, origin, n=1)

    result = disp.sync_worktrees(cfg)
    assert result["routed"] == ["T-82B"]
    assert result["skipped_by_policy"] == ["T-82A"]
    assert snap_mod.load(home, "T-82A").phase == Phase.AWAITING_CI.value
    assert snap_mod.load(home, "T-82B").phase == Phase.IMPLEMENTING.value


def test_per_repo_override_proven_through_a_real_dispatch_sweep(home, tmp_path):
    """AC4: the end-to-end path, not just the resolver or sync_worktrees
    directly -- a real dispatch(cfg, DryRunSessions(), ...) sweep (mint/due/
    spawn machinery all included) over the AC1 repro config must produce the
    same suppressed outcome, asserted on the resulting events/report."""
    origin, repo = _make_origin_and_repo(tmp_path)
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{repo}"\nbase_drift_policy = "always"\n\n'
        f'[repos.x]\npath = "{repo}"\nbase_drift_policy = "on_conflict"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))

    _seed(home, "T-82Z", Phase.AWAITING_CI, repo_name="x")
    _add_worktree(repo, home, "T-82Z", "maestro/T-82Z")
    _merge_commits_to_origin(repo, origin, n=1)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    # Untouched by drift routing -- phase unchanged and no IMPLEMENTING PhaseChanged
    # landed. (Not asserting on report.spawned here: `_seed` writes spec.md without
    # ever calling `observe-spec`, so the ticket is independently "due" on its own
    # unrelated spec-changed hash -- orthogonal to this AC's drift-routing claim.)
    assert snap_mod.load(home, "T-82Z").phase == Phase.AWAITING_CI.value
    assert "T-82Z" in report.drift_skipped
    assert report.drift_skip_reasons.get("T-82Z") == "policy"
    assert not any(e["type"] == "PhaseChanged" and e["payload"].get("phase") == Phase.IMPLEMENTING.value
                  for e in event_log.read(home, "T-82Z"))


# ---------------------------------------------------------------------------
# T-82 defect 1: the pending-CI guard trusted a stale, narrow signal -- only
# the literal string "pending". ci_state is None before the first poll and
# "unknown" on a gh auth failure / no checks registered yet / simply stale
# (sync_worktrees runs before sync_vcs each sweep) -- neither is known to be
# safe, so neither may route on drift alone.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ci_state", [None, "unknown"])
def test_unobserved_or_unknown_ci_state_blocks_drift_route_under_always(home, cfg, tmp_path, ci_state):
    """AC5: "not known to be safe" is treated as not safe -- an unobserved
    (never-polled, ci_state is None) or "unknown" (gh auth failure / no
    checks registered) CI state must block a drift-only route exactly like an
    explicit "pending" already does (test_pending_ci_checks_block_drift_route_
    under_every_mode, above), and the skip is reported with a reason
    ("checks_pending") that's distinguishable from an explicit policy skip
    ("policy")."""
    cfg.base_drift_policy = "always"
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-82C", Phase.AWAITING_CI)
    if ci_state is not None:
        event_log.append(home, "T-82C", "CiObserved",
                         {"state": ci_state, "failing_checks": []}, actor="dispatcher")
        snap_mod.rebuild(home, "T-82C")
    assert snap_mod.load(home, "T-82C").ci_state == ci_state
    _add_worktree(repo, home, "T-82C", "maestro/T-82C")
    _merge_commits_to_origin(repo, origin, n=1)

    result = disp.sync_worktrees(cfg)
    assert result["routed"] == []
    assert result["skipped_by_policy"] == ["T-82C"]
    assert result["skip_reasons"]["T-82C"] == "checks_pending"
    assert snap_mod.load(home, "T-82C").phase == Phase.AWAITING_CI.value


# ---------------------------------------------------------------------------
# T-82 defect 2: skipped_by_policy was computed by sync_worktrees and thrown
# away by dispatch() (its own hook call discarded the return value) -- no
# operator could ever see a suppressed rebase outside a direct
# sync_worktrees() call in a test.
# ---------------------------------------------------------------------------

def test_skipped_by_policy_reaches_the_dispatch_ledger(home, cfg, tmp_path):
    """AC6: the sweep record dispatch() writes to derived/dispatch.jsonl (what
    `maestro why` reads) must carry the suppressed keys and their reasons --
    not just sync_worktrees' own, easy-to-miss, in-process return value."""
    assert cfg.base_drift_policy == "on_conflict"  # the default -- a board that sets nothing
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-82L", Phase.AWAITING_CI)
    _add_worktree(repo, home, "T-82L", "maestro/T-82L")
    _merge_commits_to_origin(repo, origin, n=1)

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    ledger_path = home / "derived" / "dispatch.jsonl"
    assert ledger_path.exists()
    last = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["drift_skipped"] == ["T-82L"]
    assert last["drift_skip_reasons"].get("T-82L") == "policy"
