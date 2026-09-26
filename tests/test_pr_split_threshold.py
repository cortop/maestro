"""T-126: `pr_split_threshold` config resolution (board-wide default, per-repo
override, fail-closed on invalid) and the `pr-size` verb (`git diff --numstat`
vs the resolved base branch -- deterministic plumbing, never eyeballed by the
agent). Every test drives the real config loader / `ops.pr_size` / the real
`maestro pr-size` CLI verb over a real temp home and a real throwaway git
repo (CLAUDE.md's QA convention) -- nothing here is mocked.
"""
from __future__ import annotations

import json

import pytest

from maestro import config as config_mod, event_log, ops, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.config import Config

from conftest import git, make_origin_and_repo

# ---------------------------------------------------------------------------
# AC1: pr_split_threshold is readable board-wide and per [repos.<name>];
# unset uses the default, 0 disables, and an invalid value fails closed.
# ---------------------------------------------------------------------------

def test_pr_split_threshold_default_is_800(home):
    cfg = config_mod.load(str(home))  # no config.toml section at all
    assert cfg.pr_split_threshold == 800


def test_pr_split_threshold_board_wide_override(home):
    (home / "config.toml").write_text("[maestro]\npr_split_threshold = 500\n", encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.pr_split_threshold == 500


def test_pr_split_threshold_zero_disables(home):
    (home / "config.toml").write_text("[maestro]\npr_split_threshold = 0\n", encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.pr_split_threshold == 0


@pytest.mark.parametrize("bad", ["-1", '"nope"'])
def test_pr_split_threshold_invalid_fails_closed_board_wide(home, bad):
    (home / "config.toml").write_text(f"[maestro]\npr_split_threshold = {bad}\n",
                                      encoding="utf-8")
    with pytest.raises(store.MaestroError) as exc:
        config_mod.load(str(home))
    assert "pr_split_threshold" in str(exc.value)


def test_pr_split_threshold_per_repo_override_wins(home, tmp_path):
    from maestro import repos as repos_mod
    repo = tmp_path / "r"
    repo.mkdir()
    (home / "config.toml").write_text(
        '[maestro]\npr_split_threshold = 800\n\n'
        f'[repos.x]\npath = "{repo}"\npr_split_threshold = 1500\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.pr_split_threshold == 800
    binding = repos_mod._binding_from_table(cfg, "x", cfg.repos["x"])
    assert binding.pr_split_threshold == 1500


def test_pr_split_threshold_per_repo_unset_inherits_board_wide(home, tmp_path):
    from maestro import repos as repos_mod
    repo = tmp_path / "r"
    repo.mkdir()
    (home / "config.toml").write_text(
        '[maestro]\npr_split_threshold = 250\n\n'
        f'[repos.x]\npath = "{repo}"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    binding = repos_mod._binding_from_table(cfg, "x", cfg.repos["x"])
    assert binding.pr_split_threshold == 250


def test_pr_split_threshold_invalid_fails_closed_per_repo(home, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (home / "config.toml").write_text(
        f'[repos.x]\npath = "{repo}"\npr_split_threshold = -5\n', encoding="utf-8")
    with pytest.raises(store.MaestroError) as exc:
        config_mod.load(str(home))
    msg = str(exc.value)
    assert "[repos.x]" in msg and "pr_split_threshold" in msg


# ---------------------------------------------------------------------------
# AC2: a `maestro` verb reports lines-changed vs base and threshold-exceeded,
# exercised via cli.main([...]) over a temp home and a temp git repo.
# ---------------------------------------------------------------------------

def _seed(home, key):
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated", {"title": key}, actor="d")
    snap_mod.rebuild(home, key)


def test_pr_size_reports_zero_for_unchanged_branch(home):
    key = "T-1"
    make_origin_and_repo(home / "worktrees", name=key)
    _seed(home, key)
    cfg = Config(home=home)
    result = ops.pr_size(cfg, key)
    assert result["lines_changed"] == 0
    assert result["threshold"] == 800
    assert result["exceeds"] is False
    assert isinstance(result["tree"], str) and result["tree"]


def test_pr_size_counts_additions_vs_base(home):
    key = "T-1"
    _, repo = make_origin_and_repo(home / "worktrees", name=key)
    _seed(home, key)
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "grow", cwd=repo)
    cfg = Config(home=home)
    result = ops.pr_size(cfg, key)
    assert result["lines_changed"] == 50
    assert result["exceeds"] is False  # well under the default 800


def test_pr_size_exceeds_when_over_threshold(home):
    """Pinned by T-129: a ticket with no PR yet keeps today's behavior --
    `exceeds: true` over threshold -- unaffected by the pr-open skip added
    alongside this test."""
    key = "T-1"
    _, repo = make_origin_and_repo(home / "worktrees", name=key)
    _seed(home, key)
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "grow", cwd=repo)
    cfg = Config(home=home, pr_split_threshold=10)
    result = ops.pr_size(cfg, key)
    assert result["threshold"] == 10
    assert result["lines_changed"] == 50
    assert result["exceeds"] is True


def test_pr_size_zero_threshold_never_exceeds(home):
    key = "T-1"
    _, repo = make_origin_and_repo(home / "worktrees", name=key)
    _seed(home, key)
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(500)) + "\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "grow", cwd=repo)
    cfg = Config(home=home, pr_split_threshold=0)
    result = ops.pr_size(cfg, key)
    assert result["exceeds"] is False


def test_pr_size_different_trees_yield_different_qids(home):
    key = "T-1"
    _, repo = make_origin_and_repo(home / "worktrees", name=key)
    _seed(home, key)
    cfg = Config(home=home)
    tree_before = ops.pr_size(cfg, key)["tree"]
    (repo / "big.txt").write_text("more\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "grow", cwd=repo)
    tree_after = ops.pr_size(cfg, key)["tree"]
    assert tree_before != tree_after


def test_pr_size_skips_check_once_pr_already_open(home):
    """T-129: once a PR is open, the split-vs-single decision no longer
    applies -- a later fix round pushing the diff over threshold must not
    trigger a split proposal against a PR reviewers are already working."""
    key = "T-1"
    _, repo = make_origin_and_repo(home / "worktrees", name=key)
    _seed(home, key)
    event_log.append(home, key, "PrOpened",
                      {"number": 1, "url": "https://example/pr/1", "draft": True},
                      actor="d")
    snap_mod.rebuild(home, key)
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "grow", cwd=repo)
    cfg = Config(home=home, pr_split_threshold=10)
    result = ops.pr_size(cfg, key)
    assert result["lines_changed"] == 50
    assert result["threshold"] == 10
    assert result["exceeds"] is False
    assert result["skipped"] == "pr_open"


def test_pr_size_via_real_cli(home, capsys):
    key = "T-1"
    _, repo = make_origin_and_repo(home / "worktrees", name=key)
    _seed(home, key)
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(5)) + "\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "grow", cwd=repo)
    rc = cli_main(["--home", str(home), "pr-size", key])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["lines_changed"] == 5
    assert out["exceeds"] is False
    assert out["threshold"] == 800
