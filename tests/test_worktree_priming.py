"""GA-7 + GA-20: idempotent worktree create-or-adopt, plus priming.

A fresh `git worktree add` brings tracked files only, so a reconciler's
worktree silently lacks the repo's gitignored guidance (`CLAUDE.local.md`,
`.claude/settings.local.json`) and any installed dependency tree -- for a JS
repo whose `node_modules` is multiple GB, that means the implementing
reconciler can't even run the test command. GA-7 first fixed this as prose in
`maestro-reconcile-ready.md`; GA-20 moved the whole thing -- worktree
create-or-adopt, the CLAUDE.local.md/settings.local.json/node_modules mirror,
and a new config-declared `prime` command -- into `ops.worktree_ensure`
(`maestro worktree ensure <KEY>`), a real, idempotent Python op the ready skill
now calls with one line instead of an 8-line bash fence.

These tests drive `ops.worktree_ensure` (and, where the CLI's own exit-code/
stderr contract matters, `cli.main(["worktree", "ensure", ...])`) against a
REAL throwaway origin+repo+worktree -- never mocked -- proving: create vs.
adopt, the `$WT`/`$REPO`/`$KEY` prime environment, prime idempotence (runs
exactly once across re-runs), loud failure on a non-zero/timing-out prime, the
`mode = "local"` no-op, that `prime` is honored ONLY from config.toml (a spec
front-matter/TicketCreated `prime` field is ignored), that GA-7's
clean/write-isolated/idempotent priming guarantee still holds through the op,
and that a real dispatcher sweep never executes `prime` itself.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from maestro import config as config_mod
from maestro import event_log, ops, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.sessions import DryRunSessions

from conftest import git as _git, make_origin_and_repo as _make_origin_and_repo


def _write_config(home, repo, *, prime=None, extra="") -> config_mod.Config:
    prime_line = f'prime = {prime!r}\n' if prime is not None else ""
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{repo}"\nbranch_prefix = "maestro/"\n{prime_line}{extra}',
        encoding="utf-8")
    return config_mod.load(str(home))


def _seed_spec(home, key, *, extra_frontmatter=""):
    store.atomic_write(
        store.spec_path(home, key),
        f"# {key}\napproval_tier: 1\n{extra_frontmatter}\n## Intent\nx\n")


# ---------------------------------------------------------------------------
# Create vs. adopt
# ---------------------------------------------------------------------------

def test_ensure_creates_worktree_off_base_branch(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo)
    _seed_spec(home, "G-1")

    result = ops.worktree_ensure(cfg, "G-1")
    assert result == {"created": True, "primed": False}

    wt = store.worktree_path(home, "G-1")
    assert wt.is_dir()
    branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "maestro/G-1"


def test_ensure_adopts_existing_branch(home, tmp_path):
    """The legitimate resume case (T-44): this ticket's OWN event log already
    shows it was `implementing` before -- e.g. a prior worktree was removed
    after the reconciler already pushed commits to its branch -- so the
    fallback is allowed to re-adopt that branch."""
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo)
    _seed_spec(home, "G-2")
    event_log.append(home, "G-2", "PhaseChanged", {"phase": "implementing", "reason": "test setup"},
                     actor="test-setup")

    # A branch already exists (e.g. a prior worktree was removed after merge)
    # but the worktree dir does not.
    _git("branch", "maestro/G-2", cwd=repo)

    result = ops.worktree_ensure(cfg, "G-2")
    assert result["created"] is True
    wt = store.worktree_path(home, "G-2")
    branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "maestro/G-2"


def test_ensure_refuses_to_adopt_stale_branch_without_prior_implementing_history(home, tmp_path, capsys):
    """T-44, reproducing the 2026-08-09 shape end-to-end: a pre-existing
    `maestro/<KEY>` branch left behind by an EARLIER incarnation of this key,
    forked from a base that has since moved on -- and this ticket's own event
    log has no prior `implementing` phase, so this run has never touched that
    branch. The adopt fallback must refuse (MaestroError, no event appended,
    non-zero exit, the branch named and archive/rename suggested) rather than
    silently binding this run to a stranger's history."""
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo)
    _seed_spec(home, "R-1")

    # A branch already exists for this key -- left over from an earlier
    # incarnation -- forked from a base commit that origin has since moved
    # past.
    _git("branch", "maestro/R-1", cwd=repo)
    (repo / "README.md").write_text("main moved on\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "main moved on", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    assert event_log.read(home, "R-1") == []  # no prior implementing history

    capsys.readouterr()
    rc = cli_main(["--home", str(home), "worktree", "ensure", "R-1"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "maestro/R-1" in err
    assert "archive" in err.lower() or "rename" in err.lower()

    assert not store.worktree_path(home, "R-1").exists()
    assert event_log.read(home, "R-1") == []


def test_ensure_is_noop_when_worktree_already_exists(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo)
    _seed_spec(home, "G-3")

    ops.worktree_ensure(cfg, "G-3")
    wt = store.worktree_path(home, "G-3")
    marker_before = (wt / "README.md").stat().st_mtime

    result = ops.worktree_ensure(cfg, "G-3")
    assert result["created"] is False
    assert (wt / "README.md").stat().st_mtime == marker_before


# ---------------------------------------------------------------------------
# prime: env, idempotence, loud failure
# ---------------------------------------------------------------------------

def test_ensure_runs_prime_with_wt_repo_key_env(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    marker = tmp_path / "env.txt"
    prime = f'printf "%s|%s|%s" "$WT" "$REPO" "$KEY" > {marker}'
    cfg = _write_config(home, repo, prime=prime)
    _seed_spec(home, "G-4")

    result = ops.worktree_ensure(cfg, "G-4")
    assert result == {"created": True, "primed": True}

    wt = store.worktree_path(home, "G-4")
    assert marker.read_text() == f"{wt}|{repo}|G-4"


def test_ensure_prime_runs_exactly_once_across_reruns(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    marker = tmp_path / "log.txt"
    cfg = _write_config(home, repo, prime=f'echo ran >> {marker}')
    _seed_spec(home, "G-5")

    r1 = ops.worktree_ensure(cfg, "G-5")
    assert r1["primed"] is True
    r2 = ops.worktree_ensure(cfg, "G-5")
    assert r2["primed"] is False
    r3 = ops.worktree_ensure(cfg, "G-5")
    assert r3["primed"] is False

    assert marker.read_text().splitlines() == ["ran"]


def test_ensure_prime_nonzero_fails_loudly_no_marker(home, tmp_path, capsys):
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo, prime="exit 7")
    _seed_spec(home, "G-6")

    capsys.readouterr()
    rc = cli_main(["--home", str(home), "worktree", "ensure", "G-6"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "default" in err  # repo name
    assert "7" in err        # the rc

    # The worktree itself was created (that half succeeded); only prime failed,
    # and it failed loudly rather than silently marking itself done.
    wt = store.worktree_path(home, "G-6")
    assert wt.is_dir()
    marker = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-dir"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert not (Path(marker) / "maestro-primed").exists()

    # No phase transition -- worktree_ensure never touches the event log at all.
    assert event_log.read(home, "G-6") == []


def test_ensure_prime_timeout_fails_loudly(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo, prime="sleep 5")
    _seed_spec(home, "G-7")

    try:
        ops.worktree_ensure(cfg, "G-7", prime_timeout=1)
        assert False, "expected a MaestroError on prime timeout"
    except store.MaestroError as e:
        assert "timeout" in str(e).lower()
        assert "default" in str(e)


def test_ensure_reruns_cleanly_after_a_fixed_prime(home, tmp_path):
    """A failed prime doesn't wedge the ticket: fix config.toml and re-run --
    the worktree (already created) is adopted, and prime now runs and marks
    itself done."""
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    marker = tmp_path / "log.txt"
    cfg = _write_config(home, repo, prime="exit 3")
    _seed_spec(home, "G-8")

    try:
        ops.worktree_ensure(cfg, "G-8")
        assert False
    except store.MaestroError:
        pass

    cfg2 = _write_config(home, repo, prime=f"echo ran >> {marker}")
    result = ops.worktree_ensure(cfg2, "G-8")
    assert result == {"created": False, "primed": True}
    assert marker.read_text().splitlines() == ["ran"]


# ---------------------------------------------------------------------------
# mode = "local" (AD-6): successful no-op
# ---------------------------------------------------------------------------

def test_ensure_is_noop_for_local_mode_binding(home, tmp_path):
    target = tmp_path / "vault"
    target.mkdir()
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "/repo/default"\n\n'
        f'[repos.vault]\npath = "{target}"\nmode = "local"\nprime = "touch should-not-run"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    store.atomic_write(store.spec_path(home, "V-1"),
                       "# V-1\napproval_tier: 0\nrepo: vault\n\n## Intent\nx\n")

    result = ops.worktree_ensure(cfg, "V-1")
    assert result == {"created": False, "primed": False}
    assert not (home / "worktrees" / "V-1").exists()
    assert not (target / "should-not-run").exists()


# ---------------------------------------------------------------------------
# prime is honored ONLY from config.toml -- never a spec field or payload
# ---------------------------------------------------------------------------

def test_prime_ignored_from_spec_frontmatter_and_ticket_created_payload(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo)  # no [maestro] prime configured at all
    _seed_spec(home, "G-9", extra_frontmatter='prime: "touch spec-ran"\n')
    event_log.append(home, "G-9", "TicketCreated",
                     {"title": "G-9", "spec_hash": "x", "prime": "touch payload-ran"},
                     actor="d")
    snap_mod.rebuild(home, "G-9")

    result = ops.worktree_ensure(cfg, "G-9")
    assert result == {"created": True, "primed": False}  # binding.prime resolved to None

    wt = store.worktree_path(home, "G-9")
    assert not (wt / "spec-ran").exists()
    assert not (wt / "payload-ran").exists()
    assert not (repo / "spec-ran").exists()
    assert not (repo / "payload-ran").exists()


# ---------------------------------------------------------------------------
# GA-7's guarantee, through the op: clean, write-isolated, idempotent
# ---------------------------------------------------------------------------

def test_ensure_is_clean_isolated_and_idempotent(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    (repo / "CLAUDE.local.md").write_text("private notes\n", encoding="utf-8")
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "settings.local.json").write_text('{"allow": []}\n', encoding="utf-8")
    nm_file = repo / "node_modules" / "pkgA" / "file.txt"
    nm_file.parent.mkdir(parents=True)
    nm_file.write_text("original\n", encoding="utf-8")

    cfg = _write_config(home, repo)
    _seed_spec(home, "G-10")

    ops.worktree_ensure(cfg, "G-10")
    wt = store.worktree_path(home, "G-10")

    # --- clean: git add -A stages nothing ---
    status = subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                            capture_output=True, text=True, check=True)
    assert status.stdout == "", f"worktree not clean after ensure: {status.stdout}"
    _git("add", "-A", cwd=wt)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=wt,
                            capture_output=True, text=True, check=True)
    assert staged.stdout == "", f"primed files got staged: {staged.stdout}"

    assert (wt / "CLAUDE.local.md").read_text(encoding="utf-8") == "private notes\n"
    assert (wt / ".claude" / "settings.local.json").read_text(encoding="utf-8") == '{"allow": []}\n'
    assert (wt / "node_modules" / "pkgA" / "file.txt").read_text(encoding="utf-8") == "original\n"

    # --- write isolation: mutating the worktree's copy never reaches $REPO ---
    (wt / "node_modules" / "pkgA" / "file.txt").write_text("mutated\n", encoding="utf-8")
    assert nm_file.read_text(encoding="utf-8") == "original\n", \
        "mutating $WT/node_modules leaked into $REPO/node_modules -- not write-isolated"

    # --- idempotence: a second ensure doesn't duplicate excludes or nest node_modules ---
    ops.worktree_ensure(cfg, "G-10")
    exc = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-path", "info/exclude"],
                         capture_output=True, text=True, check=True).stdout.strip()
    exclude_lines = Path(exc).read_text(encoding="utf-8").splitlines()
    for name in ("CLAUDE.local.md", ".claude/settings.local.json", "node_modules/"):
        assert exclude_lines.count(name) == 1, \
            f"{name} appears {exclude_lines.count(name)} times in info/exclude after 2 runs"
    assert not (wt / "node_modules" / "node_modules").exists(), \
        "second run nested a node_modules inside node_modules"


def test_ensure_primed_marker_is_worktree_local_not_working_tree(home, tmp_path):
    """The primed marker lives under the worktree's OWN git dir (self-cleaning
    on `git worktree remove`), never inside the working tree -- otherwise it
    would show up as an untracked file `git add -A` stages."""
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo, prime="true")
    _seed_spec(home, "G-11")

    ops.worktree_ensure(cfg, "G-11")
    wt = store.worktree_path(home, "G-11")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                            capture_output=True, text=True, check=True)
    assert status.stdout == "", f"the primed marker leaked into the working tree: {status.stdout}"


# ---------------------------------------------------------------------------
# The dispatcher NEVER runs prime -- config-supplied shell only ever executes
# inside a reconciler session (`maestro worktree ensure`, ready.md), never
# dispatch() itself (an unsandboxed launchd daemon).
# ---------------------------------------------------------------------------

def test_cli_worktree_ensure_then_dispatch_still_routes_ready_ticket(home, tmp_path):
    """Real-surface QA: `cli.main(["worktree", "ensure", ...])` over a temp
    MAESTRO_HOME with a REAL git repo returns 0, the worktree lands on the
    expected branch, prime's side effect is on disk, and a real dispatch()
    sweep still routes a `ready` ticket to the ready phase command -- only the
    `claude` spawn itself is mocked (DryRunSessions)."""
    origin, repo = _make_origin_and_repo(tmp_path, name="dispatch-target-2")
    marker = tmp_path / "primed.txt"
    cfg = _write_config(home, repo, prime=f"touch {marker}")

    assert cli_main(["--home", str(home), "create", "GA-20 CLI QA ticket",
                     "--key", "Q-3", "--no-nudge"]) == 0
    from maestro.dispatcher import dispatch
    dispatch(cfg, DryRunSessions(), now=1000)
    assert cli_main(["--home", str(home), "set-phase", "Q-3", "ready",
                     "--reason", "test: ready for worktree setup"]) == 0

    assert cli_main(["--home", str(home), "worktree", "ensure", "Q-3"]) == 0

    wt = store.worktree_path(home, "Q-3")
    assert wt.is_dir()
    branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "maestro/Q-3"
    assert marker.exists(), "prime's side effect should be on disk"

    sessions = DryRunSessions()
    dispatch(cfg, sessions, now=2000)
    spawned = {key: prompt for key, prompt, *_ in sessions.spawned}
    assert spawned.get("Q-3") == "/maestro-reconcile-ready Q-3", (
        f"ready ticket should still route to the ready command, spawned: {spawned}")


def test_dispatch_never_executes_prime(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path, name="dispatch-target")
    marker = tmp_path / "dispatcher-ran-prime.txt"
    cfg = _write_config(home, repo, prime=f"touch {marker}")

    assert cli_main(["--home", str(home), "create", "GA-20 QA ticket",
                     "--key", "Q-2", "--no-nudge"]) == 0
    from maestro.dispatcher import dispatch
    dispatch(cfg, DryRunSessions(), now=1000)
    assert cli_main(["--home", str(home), "set-phase", "Q-2", "ready",
                     "--reason", "test: ready for worktree setup"]) == 0

    sessions = DryRunSessions()
    dispatch(cfg, sessions, now=2000)
    spawned = {key: prompt for key, prompt, *_ in sessions.spawned}
    assert spawned.get("Q-2") == "/maestro-reconcile-ready Q-2"

    assert not marker.exists(), "dispatch() executed config-supplied `prime` shell directly"
    assert not (home / "worktrees" / "Q-2").exists(), \
        "dispatch() must never create the worktree either -- only the reconciler session does"


# ---------------------------------------------------------------------------
# MTO-1: an interrupted `worktree ensure` can never be mistaken for complete.
#
# `_GIT_TIMEOUT` (the short, 30s plumbing timeout) killed a real ~230k-file
# monorepo checkout (measured ~56s) mid-write -- after files landed, before
# the index did. The old `wt.exists()` gate then treated that half-built
# directory as done forever. These tests prove the postcondition-based
# replacement: a broken existing worktree is torn down and rebuilt, a timeout
# on `worktree add` itself surfaces loudly with no partial directory left
# behind, `worktree add` runs under a much larger config-declared timeout
# (never the short one), and a partial `node_modules` sync completes on retry.
# ---------------------------------------------------------------------------

def _seed_many_tracked_files(repo, n):
    for i in range(n):
        (repo / f"file{i}.txt").write_text(f"content {i}\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add many files", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)


def test_worktree_health_missing_and_no_index(home, tmp_path):
    """`worktree_health` itself: missing dir, and a dir that exists but was
    never a real git worktree (no index at all) -- the exact false-positive
    the old bare `wt.exists()` check fell for."""
    wt = home / "worktrees" / "no-such-key"
    assert ops.worktree_health(wt) == {"healthy": False, "reason": "missing"}

    wt.mkdir(parents=True)
    (wt / "some-file.txt").write_text("looks like something got written here\n")
    assert ops.worktree_health(wt) == {"healthy": False, "reason": "no index"}


def test_ensure_rebuilds_a_worktree_dir_with_no_index(home, tmp_path):
    """AC1: a worktree directory that EXISTS but is not a real git worktree at
    all is not mistaken for complete -- ensure tears it down and creates a
    real one, rather than reporting `created: False` with exit 0 on a broken
    directory (the pre-MTO-1 bug's exact shape)."""
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo)
    _seed_spec(home, "H-1")

    wt = store.worktree_path(home, "H-1")
    wt.mkdir(parents=True)
    (wt / "some-file.txt").write_text("looks like something got written here\n")

    result = ops.worktree_ensure(cfg, "H-1")
    assert result["created"] is True
    assert ops.worktree_health(wt) == {"healthy": True, "reason": None}
    branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "maestro/H-1"


def test_ensure_rebuilds_a_worktree_with_a_truncated_checkout(home, tmp_path):
    """AC1 (the real reproduction, MTO-1): a worktree dir that exists and even
    has a real index, but whose checkout was interrupted -- files the index
    references are missing on disk, the exact `git status` signature of the
    2026-08-10 incident (229,695 staged deletions on a real ~230k-file repo,
    scaled down here to stay in-tree per the ticket's testing note)."""
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    _seed_many_tracked_files(repo, 60)
    cfg = _write_config(home, repo)
    _seed_spec(home, "H-2")

    ops.worktree_ensure(cfg, "H-2")
    wt = store.worktree_path(home, "H-2")
    assert ops.worktree_health(wt)["healthy"] is True

    # Simulate a checkout killed partway through: 55 of the 60 tracked files
    # never got written, but the index (written first) still references them
    # -- `git status` now reports them "deleted".
    for i in range(55):
        (wt / f"file{i}.txt").unlink()
    assert ops.worktree_health(wt) == {"healthy": False, "reason": "mass deletion in git status"}

    result = ops.worktree_ensure(cfg, "H-2")
    assert result["created"] is True
    assert ops.worktree_health(wt) == {"healthy": True, "reason": None}
    for i in range(60):
        assert (wt / f"file{i}.txt").exists()
    status = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                            capture_output=True, text=True, check=True)
    assert status.stdout == ""


def test_ensure_timeout_on_worktree_add_raises_loudly_and_leaves_no_partial_dir(
        home, tmp_path, monkeypatch, capsys):
    """AC2: a `TimeoutExpired` from `git worktree add` surfaces as
    `store.MaestroError` (non-zero CLI exit), appends no event, and leaves no
    partial worktree directory behind -- never the raw traceback the old code
    let escape, which left a poison-pill directory for every retry to skip."""
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo, extra="worktree_timeout = 1\n")
    _seed_spec(home, "H-4")
    wt = store.worktree_path(home, "H-4")

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:5] == ["git", "-C", str(repo), "worktree", "add"] and "-b" in cmd:
            # A killed `git worktree add` typically wrote SOME files before
            # being SIGKILLed -- simulate that partial artifact so the
            # cleanup path this test is proving has something real to remove.
            wt.mkdir(parents=True, exist_ok=True)
            (wt / "partial-file.txt").write_text("half a checkout\n")
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    capsys.readouterr()
    rc = cli_main(["--home", str(home), "worktree", "ensure", "H-4"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "timeout" in err.lower()

    assert not wt.exists()
    assert event_log.read(home, "H-4") == []


def test_worktree_add_uses_configured_worktree_timeout_not_the_short_git_timeout(
        home, tmp_path, monkeypatch):
    """AC3: `worktree add` runs under the config-declared `worktree_timeout`
    (default 600s -- generous headroom above the ~56s a real ~230k-file
    checkout measured), never the short 30s plumbing timeout every other git
    call in this module uses."""
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo, extra="worktree_timeout = 5\n")
    assert cfg.worktree_timeout == 5
    _seed_spec(home, "H-3")

    seen_timeouts = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        if cmd[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            seen_timeouts.append(kwargs.get("timeout"))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    ops.worktree_ensure(cfg, "H-3")

    assert seen_timeouts == [5]  # the configured worktree_timeout, not 30 or 600


def test_worktree_timeout_defaults_to_600(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo)
    assert cfg.worktree_timeout == 600


def test_ensure_completes_a_partial_node_modules_sync_on_retry(home, tmp_path):
    """AC4: a partially-copied `node_modules` (the old gate was bare
    `dst_nm.exists()`, so a truncated copy from a killed/timed-out earlier
    attempt was skipped forever) is completed on the next ensure -- driven by
    a completion marker written only after the sync succeeds, never by
    directory existence."""
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    nm = repo / "node_modules"
    (nm / "pkgA").mkdir(parents=True)
    (nm / "pkgA" / "a.txt").write_text("a\n")
    (nm / "pkgB").mkdir(parents=True)
    (nm / "pkgB" / "b.txt").write_text("b\n")

    cfg = _write_config(home, repo)
    _seed_spec(home, "H-5")

    ops.worktree_ensure(cfg, "H-5")
    wt = store.worktree_path(home, "H-5")
    marker = ops._git_dir(wt) / ops._NODE_MODULES_SYNCED_MARKER
    assert marker.exists()
    assert (wt / "node_modules" / "pkgB" / "b.txt").read_text() == "b\n"

    # Simulate an earlier attempt that got killed partway through the sync:
    # pkgB never landed, and (since it never returned 0) the marker was never
    # written -- delete both to reproduce that state honestly.
    marker.unlink()
    shutil.rmtree(wt / "node_modules" / "pkgB")
    assert not (wt / "node_modules" / "pkgB").exists()

    ops.worktree_ensure(cfg, "H-5")
    assert (wt / "node_modules" / "pkgA" / "a.txt").exists()
    assert (wt / "node_modules" / "pkgB" / "b.txt").read_text() == "b\n"
    assert marker.exists()


def test_ensure_interrupted_create_then_retry_never_reports_success_on_broken_worktree(
        home, tmp_path, monkeypatch, capsys):
    """AC5 -- the regression test the ticket asks for, driving the real CLI
    (`cli_main(["worktree", "ensure", ...])`, not `ops.worktree_ensure`
    directly) over a temp MAESTRO_HOME: an interrupted create (simulated
    timeout) fails loudly with no partial directory left; the retry that
    follows does the full real create and only THEN reports success -- at no
    point does any call report `created: False`/exit 0 on top of a broken
    worktree."""
    origin, repo = _make_origin_and_repo(tmp_path, name="target")
    cfg = _write_config(home, repo, extra="worktree_timeout = 1\n")
    _seed_spec(home, "H-6")
    wt = store.worktree_path(home, "H-6")

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:5] == ["git", "-C", str(repo), "worktree", "add"] and "-b" in cmd:
            wt.mkdir(parents=True, exist_ok=True)
            (wt / "partial-file.txt").write_text("half a checkout\n")
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    capsys.readouterr()
    rc1 = cli_main(["--home", str(home), "worktree", "ensure", "H-6"])
    assert rc1 != 0, "the interrupted create must fail loudly, never exit 0"
    err = capsys.readouterr().err
    assert "timeout" in err.lower()
    assert not wt.exists(), "the interrupted attempt must leave no partial directory"

    monkeypatch.undo()  # restore the real subprocess.run for the retry

    capsys.readouterr()
    rc2 = cli_main(["--home", str(home), "worktree", "ensure", "H-6"])
    assert rc2 == 0
    out = capsys.readouterr().out
    assert '"created": true' in out, (
        f"the retry must report the real create it just did, not a stale/skipped no-op: {out!r}")
    assert ops.worktree_health(wt) == {"healthy": True, "reason": None}
    branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "maestro/H-6"
