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


# --- AC5: blocking is scoped to the spawn step, proven by side effects ------

def test_blocked_sweep_skips_only_sync_worktrees_and_spawn(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path)
    # Block the repo via a hand-written sentinel — NOT a real conflicting merge —
    # so local main's tip stays exactly what was pushed, and thus a genuine
    # ancestor of origin/main once another commit lands there (the ff-only
    # scenario this test needs; AC1/AC2 above already prove blocker detection
    # against a real conflicting merge).
    gitdir = _gitdir(repo)
    (gitdir / "MERGE_HEAD").write_text("0" * 40 + "\n")

    _push_extra_commit_from_new_clone(origin, home)  # repo is now behind origin/main

    local_main_before = _rev_parse(repo, "main")
    fetch_head = repo / ".git" / "FETCH_HEAD"
    assert not fetch_head.exists()

    cfg = Config(home=home, repo_path=str(repo), max_concurrency=3,
                min_spawn_interval=0, backup_interval=1)
    inbox.append_new(home, "New ticket", key="T-9", args={"approval_tier": 0})
    _seed_due_ticket(home, "T-1")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.repo_blockers
    assert report.spawned == []

    # mint still ran
    assert "T-9" in disp.list_keys(home)
    assert (home / "events" / "T-9.jsonl").exists()
    # backup still ran
    assert list(backup.resolve_backup_dir(cfg).glob("*.tar.gz"))
    # heartbeat still written, with the blockers recorded
    hb = store.read_json(home / "derived" / ".heartbeat.json", {})
    assert hb["repo_blockers"]

    # sync_worktrees was skipped: no fetch/merge touched the broken repo
    assert _rev_parse(repo, "main") == local_main_before
    assert not fetch_head.exists()

    # repair, then the very same sweep DOES fast-forward + fetch
    (gitdir / "MERGE_HEAD").unlink()
    report2 = disp.dispatch(cfg, DryRunSessions(), now=2000)
    assert report2.repo_blockers == []
    assert fetch_head.exists()
    assert _rev_parse(repo, "main") != local_main_before


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
