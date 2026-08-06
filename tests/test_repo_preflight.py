"""Dispatcher preflight (T-19): refuse to fast-forward/spawn into ``cfg.repo_path``
while it is mid-merge/rebase or carries a genuine (not merely quoted) conflict
hunk. Every scenario drives a real git repo under ``tmp_path`` — never a mock
or a monkeypatched ``subprocess`` — because the whole point of the guard is to
survive real git state, and the ``--all-match`` grep guard specifically exists
so that this file's own hand-written conflict-marker fixtures can never brick
the fleet.
"""
import json
import subprocess

import pytest

from maestro import backup, cli, dispatcher as disp, event_log, inbox, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git as _git, make_origin_and_repo as _make_origin_and_repo


def _rev_parse(repo, ref):
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _commit(path, name, content, msg=None):
    (path / name).write_text(content)
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", msg or f"add {name}", cwd=path)


def _gitdir(repo):
    from pathlib import Path
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def _conflicting_merge_repo(tmp_path, name="repo"):
    """A real repo mid-merge with a genuine, unresolved conflict in f.txt."""
    repo = tmp_path / name
    _init_repo(repo)
    _commit(repo, "f.txt", "base\n", "base")
    _git("checkout", "-q", "-b", "feature", cwd=repo)
    _commit(repo, "f.txt", "feature change\n", "feature change")
    _git("checkout", "-q", "main", cwd=repo)
    _commit(repo, "f.txt", "main change\n", "main change")
    subprocess.run(["git", "merge", "feature"], cwd=repo, capture_output=True, text=True)
    assert (repo / ".git" / "MERGE_HEAD").exists()  # sanity: the merge really conflicted
    return repo


def _push_extra_commit_from_new_clone(origin, tmp_path):
    """Simulate another ticket's PR merging, from an independent clone (the
    primary ``repo`` may itself be mid-conflict and unable to commit/push)."""
    other = tmp_path / "other-clone"
    _git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=other)
    _git("config", "user.name", "Test", cwd=other)
    (other / "NEWS.md").write_text("merged change\n")
    _git("add", "-A", cwd=other)
    _git("commit", "-q", "-m", "external change", cwd=other)
    _git("push", "-q", "origin", "main", cwd=other)


def _seed_due_ticket(home, key="T-1"):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, key)


# --- AC1/AC2: repo_preflight() itself, real git state only ------------------

def test_repo_preflight_real_conflicting_merge_blocks_with_all_three_probes(home, tmp_path):
    repo = _conflicting_merge_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo))
    verdict = disp.repo_preflight(cfg)
    assert verdict["ok"] is False
    assert "MERGE_HEAD" in verdict["blockers"]
    assert any("ls-files -u" in b for b in verdict["blockers"])
    assert any(b.startswith("conflict markers in:") and "f.txt" in b for b in verdict["blockers"])


@pytest.mark.parametrize("sentinel,is_dir", [
    ("CHERRY_PICK_HEAD", False),
    ("REVERT_HEAD", False),
    ("rebase-merge", True),
    ("rebase-apply", True),
])
def test_repo_preflight_detects_each_sentinel(home, tmp_path, sentinel, is_dir):
    repo = tmp_path / f"repo-{sentinel}"
    _init_repo(repo)
    _commit(repo, "f.txt", "hi\n", "init")
    gitdir = _gitdir(repo)
    if is_dir:
        (gitdir / sentinel).mkdir()
    else:
        (gitdir / sentinel).write_text("deadbeef\n")

    cfg = Config(home=home, repo_path=str(repo))
    verdict = disp.repo_preflight(cfg)
    assert verdict["ok"] is False
    expect = f"{sentinel}/" if is_dir else sentinel
    assert expect in verdict["blockers"]


def test_repo_preflight_quoted_marker_alone_does_not_block(home, tmp_path):
    """--all-match is load-bearing: a doc that merely QUOTES a marker (like
    this feature's own tests/docs) must not brick the fleet."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "DESIGN.md", "Example:\n<<<<<<< HEAD\n(that's the only marker)\n",
            "doc quoting one marker")
    cfg = Config(home=home, repo_path=str(repo))
    assert disp.repo_preflight(cfg) == {"ok": True, "blockers": []}


def test_repo_preflight_real_conflict_hunk_blocks(home, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "conflicted.txt",
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n",
            "hand-written conflict hunk")
    cfg = Config(home=home, repo_path=str(repo))
    verdict = disp.repo_preflight(cfg)
    assert verdict["ok"] is False
    assert any("conflicted.txt" in b for b in verdict["blockers"])


# --- AC3: a real dispatch() sweep skips the spawn step only -----------------

def test_dispatch_blocks_spawn_when_repo_conflicted_then_spawns_once_repaired(home, tmp_path):
    repo = _conflicting_merge_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo), max_concurrency=3, min_spawn_interval=0)
    _seed_due_ticket(home, "T-1")

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert report.spawned == []
    assert sessions.spawned == []
    assert "T-1" in [k for k, _ in report.due]
    assert report.repo_blockers

    _git("merge", "--abort", cwd=repo)
    report2 = disp.dispatch(cfg, sessions, now=2000)
    assert report2.spawned == ["T-1"]
    assert report2.repo_blockers == []


def test_dispatch_blocks_spawn_even_for_unthrottled_due_reason(home, tmp_path):
    """A human answering a question (an _UNTHROTTLED_REASONS due-reason) must
    still be blocked — preflight sits outside the spawn-rate floor entirely."""
    repo = _conflicting_merge_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo), max_concurrency=3)

    store.atomic_write(store.spec_path(home, "T-2"), "# T-2\napproval_tier: 0\n")
    event_log.append(home, "T-2", "TicketCreated", {"title": "T-2"}, actor="d")
    event_log.append(home, "T-2", "PhaseChanged", {"phase": Phase.AWAITING_HUMAN.value}, actor="r")
    event_log.append(home, "T-2", "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r")
    snap_mod.rebuild(home, "T-2")
    inbox.append_command(home, "T-2", "ans", {"text": "yes", "qid": "q1"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    reasons = dict(report.due)
    assert reasons.get("T-2") == "inbox"
    assert report.spawned == []


# --- AC4: merely-dirty is NOT blocked ----------------------------------------

def test_dirty_repo_is_not_blocked(home, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "tracked.txt", "hello\n", "init")

    (repo / "tracked.txt").write_text("modified but uncommitted\n")            # dirty tracked file
    (repo / "staged.txt").write_text("staged\n")
    _git("add", "staged.txt", cwd=repo)                                         # staged, uncommitted
    (repo / "untracked_conflict.txt").write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")               # untracked, full hunk
    _git("checkout", "-q", "--detach", "main", cwd=repo)                       # detached HEAD

    cfg = Config(home=home, repo_path=str(repo), max_concurrency=3, min_spawn_interval=0)
    _seed_due_ticket(home, "T-1")
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.repo_blockers == []
    assert report.spawned == ["T-1"]


# --- AC5/MR-5 AC1: blocking is scoped to the AFFECTED REPO's spawn+sync step,
# never the whole board -- proven with two real repos, one blocked. Replaces
# the pre-MR-5 whole-board pin (this file's own history) with per-repo
# semantics: the CONTRACT CHANGE this ticket states up front.

def _repo_table(path, *, base_branch="main", default=False):
    return {"path": str(path), "slug": None, "base_branch": base_branch,
            "branch_prefix": "maestro/", "default": default, "max_spawns_per_sweep": None}


def _seed_bound_ticket(home, key, repo_name, phase=Phase.IMPLEMENTING):
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\napproval_tier: 0\nrepo: {repo_name}\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "repo": repo_name,
                      "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(home, key)


def test_blocked_repo_skips_only_its_own_spawn_and_sync_two_repo(home, tmp_path):
    alpha_origin, alpha_repo = _make_origin_and_repo(tmp_path, "alpha", base_branch="main")
    beta_origin, beta_repo = _make_origin_and_repo(tmp_path, "beta", base_branch="main")

    # Block alpha via a hand-written sentinel — NOT a real conflicting merge —
    # so its local main tip stays a genuine ancestor of origin/main once
    # another commit lands there (the ff-only scenario this test needs; AC1/AC2
    # above already prove blocker detection against a real conflicting merge).
    gitdir = _gitdir(alpha_repo)
    (gitdir / "MERGE_HEAD").write_text("0" * 40 + "\n")
    _push_extra_commit_from_new_clone(alpha_origin, tmp_path)  # alpha now behind origin/main

    alpha_main_before = _rev_parse(alpha_repo, "main")
    alpha_fetch_head = alpha_repo / ".git" / "FETCH_HEAD"
    beta_fetch_head = beta_repo / ".git" / "FETCH_HEAD"
    assert not alpha_fetch_head.exists()
    assert not beta_fetch_head.exists()

    cfg = Config(home=home, max_concurrency=3, min_spawn_interval=0, backup_interval=1,
                repos={"alpha": _repo_table(alpha_repo, default=True),
                       "beta": _repo_table(beta_repo)})

    _seed_bound_ticket(home, "A-1", "alpha")                       # due, repo blocked
    _seed_bound_ticket(home, "B-1", "beta")                        # due, repo healthy
    _seed_bound_ticket(home, "B-2", "beta", phase=Phase.AWAITING_CI)  # forces beta into
    _add_worktree(beta_repo, home, "B-2", "maestro/B-2")              # sync_worktrees' groups

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    # spawn gate: only alpha's due ticket is skipped, beta's spawns
    assert report.spawned == ["B-1"]
    assert "A-1" in [k for k, _ in report.due]          # stays due, not dropped
    assert report.repo_blockers_by_repo.keys() == {"alpha"}
    assert "MERGE_HEAD" in report.repo_blockers_by_repo["alpha"]
    assert set(report.repo_blockers) == set(report.repo_blockers_by_repo["alpha"])

    # sync_worktrees: alpha's group was skipped entirely, beta's fetched
    assert _rev_parse(alpha_repo, "main") == alpha_main_before
    assert not alpha_fetch_head.exists()
    assert beta_fetch_head.exists()

    # repair alpha, next sweep spawns it and fast-forwards it too
    (gitdir / "MERGE_HEAD").unlink()
    report2 = disp.dispatch(cfg, DryRunSessions(), now=2000)
    assert "A-1" in report2.spawned
    assert report2.repo_blockers_by_repo == {}
    assert alpha_fetch_head.exists()
    assert _rev_parse(alpha_repo, "main") != alpha_main_before


def _add_worktree(repo, home, key, branch, base="main"):
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", branch,
                    str(store.worktree_path(home, key)), base], check=True,
                   capture_output=True, text=True)


def test_all_repos_blocked_still_mints_and_backs_up(home, tmp_path):
    """AC4: with every referenced repo blocked, the never-halt-siblings ticks
    (mint, backup) still run -- re-proving T-19's invariant under the new
    per-repo gate."""
    repo = _conflicting_merge_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo), max_concurrency=3,
                min_spawn_interval=0, backup_interval=1)
    inbox.append_new(home, "New ticket", key="T-9", args={"approval_tier": 0})
    _seed_due_ticket(home, "T-1")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.spawned == []
    assert report.repo_blockers_by_repo

    assert "T-9" in disp.list_keys(home)
    assert (home / "events" / "T-9.jsonl").exists()
    assert list(backup.resolve_backup_dir(cfg).glob("*.tar.gz"))


def test_unreferenced_configured_repo_produces_no_mapping_entry(home, tmp_path):
    """AC4: a configured-but-unreferenced [repos.gamma] with a nonexistent path
    is a total no-op -- no probe, no mapping entry, no effect on spawns."""
    _origin, repo = _make_origin_and_repo(tmp_path, "healthy")
    cfg = Config(home=home, repo_path=str(repo), max_concurrency=3, min_spawn_interval=0,
                repos={"gamma": _repo_table(tmp_path / "does-not-exist")})
    _seed_due_ticket(home, "T-1")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.spawned == ["T-1"]
    assert "gamma" not in report.repo_blockers_by_repo
    hb = store.read_json(home / "derived" / ".heartbeat.json", {})
    assert "gamma" not in hb["repo_blockers_by_repo"]


# --- AC6: both CLI surfaces --------------------------------------------------

def test_dispatch_cli_reports_repo_blocked(home, tmp_path, capsys):
    repo = _conflicting_merge_repo(tmp_path)
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{repo}"\nmin_spawn_interval = 0\n')
    _seed_due_ticket(home, "T-1")

    rc = cli.main(["--home", str(home), "dispatch", "--dry-run"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["repo_blocked"]
    assert out["would_spawn"] == []
    assert (home / "derived" / "WORKSTATE.md").exists()


def test_doctor_cli_computes_repo_preflight_live(home, tmp_path, capsys):
    repo = _conflicting_merge_repo(tmp_path)
    (home / "config.toml").write_text(f'[maestro]\nrepo_path = "{repo}"\nmin_spawn_interval = 0\n')
    _seed_due_ticket(home, "T-1")
    cli.main(["--home", str(home), "dispatch", "--dry-run"])
    capsys.readouterr()

    rc = cli.main(["--home", str(home), "doctor"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["repo_preflight"]["ok"] is False
    assert out["heartbeat"]["repo_blockers"]

    # Repair WITHOUT another sweep: doctor flips live, heartbeat keeps the old verdict.
    _git("merge", "--abort", cwd=repo)
    rc2 = cli.main(["--home", str(home), "doctor"])
    assert rc2 == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["repo_preflight"]["ok"] is True
    assert out2["heartbeat"]["repo_blockers"]  # still the stale, last-sweep verdict


def test_ans_cli_warns_on_stderr_when_repo_blocked(home, tmp_path, capsys):
    repo = _conflicting_merge_repo(tmp_path)
    (home / "config.toml").write_text(f'[maestro]\nrepo_path = "{repo}"\n')

    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: 0\n")
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.AWAITING_HUMAN.value}, actor="r")
    event_log.append(home, "T-1", "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r")
    snap_mod.rebuild(home, "T-1")

    rc = cli.main(["--home", str(home), "ans", "T-1", "yes", "--qid", "q1"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "blocked" in err
    assert "MERGE_HEAD" in err


# --- MR-5 AC2: heartbeat/doctor/dispatch/why/ans per-repo attribution -------

def test_heartbeat_keeps_flat_list_and_carries_new_per_repo_key(home, tmp_path):
    repo = _conflicting_merge_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo), max_concurrency=3, min_spawn_interval=0)
    _seed_due_ticket(home, "T-1")

    disp.dispatch(cfg, DryRunSessions(), now=1000)
    hb = store.read_json(home / "derived" / ".heartbeat.json", {})
    assert hb["repo_blockers"]                       # unchanged flat union, byte-compatible
    assert hb["repo_blockers_by_repo"]                # NEW per-repo mapping
    assert "MERGE_HEAD" in hb["repo_blockers_by_repo"]["default"]
    assert set(hb["repo_blockers"]) == set(hb["repo_blockers_by_repo"]["default"])


def test_dispatch_and_doctor_and_why_render_per_repo_attribution(home, tmp_path, capsys):
    alpha_repo = _conflicting_merge_repo(tmp_path, name="alpha")
    _origin, beta_repo = _make_origin_and_repo(tmp_path, "beta")
    (home / "config.toml").write_text(
        "[maestro]\nmin_spawn_interval = 0\n\n"
        f'[repos.alpha]\npath = "{alpha_repo}"\ndefault = true\n\n'
        f'[repos.beta]\npath = "{beta_repo}"\n')
    from maestro.config import load
    cfg = load(str(home))
    _seed_bound_ticket(home, "A-1", "alpha")
    _seed_bound_ticket(home, "B-1", "beta")

    rc = cli.main(["--home", str(home), "dispatch", "--dry-run"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["repo_blocked"]                                  # flat, unchanged shape
    assert out["repo_blocked_by_repo"].keys() == {"alpha"}
    assert "MERGE_HEAD" in out["repo_blocked_by_repo"]["alpha"]
    assert out["would_spawn"] == ["B-1"]

    rc2 = cli.main(["--home", str(home), "doctor"])
    assert rc2 == 0
    doc = json.loads(capsys.readouterr().out)
    check = next(c for c in doc["checks"] if c["name"] == "repo_preflight")
    assert check["status"] == "fail"
    assert check["blockers_by_repo"].keys() == {"alpha"}

    rc3 = cli.main(["--home", str(home), "why", "A-1"])
    assert rc3 == 0
    why = json.loads(capsys.readouterr().out)
    outcomes = {d["outcome"] for d in why["decisions"]}
    assert "repo_blocked" in outcomes


def test_ans_cli_warns_naming_the_specific_blocked_repo(home, tmp_path, capsys):
    alpha_repo = _conflicting_merge_repo(tmp_path, name="alpha")
    _origin, beta_repo = _make_origin_and_repo(tmp_path, "beta")
    (home / "config.toml").write_text(
        f'[maestro]\n\n[repos.alpha]\npath = "{alpha_repo}"\ndefault = true\n\n'
        f'[repos.beta]\npath = "{beta_repo}"\n')

    store.atomic_write(store.spec_path(home, "A-1"), "# A-1\napproval_tier: 0\nrepo: alpha\n")
    event_log.append(home, "A-1", "TicketCreated", {"title": "A-1", "repo": "alpha"}, actor="d")
    event_log.append(home, "A-1", "PhaseChanged", {"phase": Phase.AWAITING_HUMAN.value}, actor="r")
    event_log.append(home, "A-1", "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r")
    snap_mod.rebuild(home, "A-1")

    rc = cli.main(["--home", str(home), "ans", "A-1", "yes", "--qid", "q1"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "alpha" in err
    assert "MERGE_HEAD" in err


# --- MR-5 AC3: max_spawns_per_sweep caps one repo without starving others ---

def test_max_spawns_per_sweep_caps_one_repo_not_others(home, tmp_path):
    _alpha_origin, alpha_repo = _make_origin_and_repo(tmp_path, "alpha")
    _beta_origin, beta_repo = _make_origin_and_repo(tmp_path, "beta")
    beta_table = _repo_table(beta_repo)
    beta_table["max_spawns_per_sweep"] = 1
    cfg = Config(home=home, max_concurrency=10, min_spawn_interval=0,
                repos={"alpha": _repo_table(alpha_repo, default=True), "beta": beta_table})

    _seed_bound_ticket(home, "A-1", "alpha")
    _seed_bound_ticket(home, "B-1", "beta")
    _seed_bound_ticket(home, "B-2", "beta")
    _seed_bound_ticket(home, "B-3", "beta")

    # A single, reused SessionManager -- like the real fleet, a spawned key
    # stays "active" (claimed) on the very next sweep instead of immediately
    # coming back due, which is what lets the cap's SECOND sweep reach a
    # previously-skipped key instead of respawning the same first one.
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert "A-1" in report.spawned
    spawned_beta = [k for k in report.spawned if k.startswith("B-")]
    assert len(spawned_beta) == 1
    assert len(report.spawned) == 2

    skipped_beta = [k for k in ("B-1", "B-2", "B-3") if k not in report.spawned]
    assert len(skipped_beta) == 2
    due_keys = {k for k, _ in report.due}
    for k in skipped_beta:
        assert k in due_keys                                    # still due, not dropped
    decision = disp.key_decisions(home, skipped_beta[0], tail=1)[0]
    assert decision["outcome"] == "repo_capped"

    # the very next sweep gives a skipped key its turn (spawn floor is 0)
    report2 = disp.dispatch(cfg, sessions, now=1001)
    assert any(k in report2.spawned for k in skipped_beta)


def test_no_cap_set_spawns_all_four_in_one_sweep(home, tmp_path):
    _alpha_origin, alpha_repo = _make_origin_and_repo(tmp_path, "alpha")
    _beta_origin, beta_repo = _make_origin_and_repo(tmp_path, "beta")
    cfg = Config(home=home, max_concurrency=10, min_spawn_interval=0,
                repos={"alpha": _repo_table(alpha_repo, default=True),
                       "beta": _repo_table(beta_repo)})   # uncapped

    _seed_bound_ticket(home, "A-1", "alpha")
    _seed_bound_ticket(home, "B-1", "beta")
    _seed_bound_ticket(home, "B-2", "beta")
    _seed_bound_ticket(home, "B-3", "beta")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert set(report.spawned) == {"A-1", "B-1", "B-2", "B-3"}


# --- MR-5 AC6: unknown-repo-name + missing-reconcile-skill are WARNINGS -----

def test_doctor_warns_on_unknown_repo_name_and_missing_reconcile_skill(home, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True,
                   capture_output=True, text=True)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "f.txt").write_text("hi\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    # no .claude/commands/maestro-reconcile.md anywhere in `repo`

    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{repo}"\nmin_spawn_interval = 0\n')
    # A spec naming an unconfigured repo (typo/unknown) -- resolves to the
    # implicit default per repos.resolve(), so the sweep still spawns it.
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 0\nrepo: typo-repo\n")
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "T-1", "repo": "typo-repo",
                      "spec_hash": disp.spec_hash_on_disk(home, "T-1")}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    from maestro.config import load
    cfg = load(str(home))
    real_report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert real_report.spawned == ["T-1"]                        # warning never blocks the spawn

    rc = cli.main(["--home", str(home), "doctor"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    unknown_check = next(c for c in out["checks"] if c["name"] == "unknown_repo_bindings")
    assert unknown_check["status"] == "warn"
    assert {u["key"] for u in unknown_check["unknown"]} == {"T-1"}

    skill_check = next(c for c in out["checks"] if c["name"] == "missing_reconcile_skill")
    assert skill_check["status"] == "warn"
    assert "default" in skill_check["missing"]


# --- AC7: config knob + fail-open ---------------------------------------------

def test_repo_preflight_false_skips_check_entirely(home, tmp_path):
    repo = _conflicting_merge_repo(tmp_path)
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{repo}"\nmin_spawn_interval = 0\nrepo_preflight = false\n')
    _seed_due_ticket(home, "T-1")

    from maestro.config import load
    cfg = load(str(home))
    assert cfg.repo_preflight is False
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.repo_blockers == []
    assert report.spawned == ["T-1"]


def test_repo_preflight_knob_in_default_config_toml():
    from maestro.config import DEFAULT_CONFIG_TOML
    assert "repo_preflight" in DEFAULT_CONFIG_TOML


@pytest.mark.parametrize("build_repo_path", [
    lambda tmp_path: None,                                    # unset
    lambda tmp_path: str(tmp_path / "does-not-exist"),         # absent
    lambda tmp_path: str(_new_plain_dir(tmp_path)),             # not a repo at all
])
def test_repo_preflight_fails_open_on_unusable_repo(home, tmp_path, build_repo_path):
    cfg = Config(home=home, repo_path=build_repo_path(tmp_path))
    verdict = disp.repo_preflight(cfg)
    assert verdict["ok"] is True
    assert verdict["blockers"] == ["preflight-unavailable"]


def _new_plain_dir(tmp_path):
    d = tmp_path / "not-a-repo"
    d.mkdir()
    return d


def test_repo_preflight_fails_open_on_unborn_head(home, tmp_path):
    repo = tmp_path / "unborn"
    _init_repo(repo)  # git init, no commits — HEAD is unborn
    cfg = Config(home=home, repo_path=str(repo))
    verdict = disp.repo_preflight(cfg)
    assert verdict == {"ok": True, "blockers": ["preflight-unavailable"]}


def test_repo_preflight_fails_open_when_git_missing_from_path(home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "f.txt", "hi\n", "init")
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))  # a real PATH with no git on it
    cfg = Config(home=home, repo_path=str(repo))
    verdict = disp.repo_preflight(cfg)
    assert verdict == {"ok": True, "blockers": ["preflight-unavailable"]}


def test_unusable_repo_spawns_normally(home, tmp_path):
    """Fail-open means a due ticket still spawns, not just that ok=True."""
    cfg = Config(home=home, repo_path=str(tmp_path / "nope"),
                max_concurrency=3, min_spawn_interval=0)
    _seed_due_ticket(home, "T-1")
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.spawned == ["T-1"]
    assert report.repo_blockers == []
