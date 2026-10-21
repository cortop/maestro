"""T-191: `maestro pr-size` reports `split_viable` -- whether the changed files
can be grouped into a balanced stack (smallest entry >= 1/3 of the largest).
Driven through the real CLI over a real temp home and git repo."""
from __future__ import annotations

import json

from maestro import event_log, snapshot as snap_mod, store
from maestro.cli import main as cli_main

from conftest import git, make_origin_and_repo

from test_reconcile_skill import _commands_path, _skills_path, _strip_frontmatter


def _setup(home, sizes, threshold=100, pr_open=False):
    key = "T-1"
    _, repo = make_origin_and_repo(home / "worktrees", name=key)
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated", {"title": key}, actor="d")
    if pr_open:
        event_log.append(home, key, "PrOpened",
                         {"number": 1, "url": "https://example/pr/1", "draft": True},
                         actor="d")
    snap_mod.rebuild(home, key)
    (home / "config.toml").write_text(f"[maestro]\npr_split_threshold = {threshold}\n",
                                      encoding="utf-8")
    for i, n in enumerate(sizes):
        (repo / f"f{i}.txt").write_text("".join(f"l{j}\n" for j in range(n)))
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "grow", cwd=repo)
    return key


def _pr_size(home, capsys, key):
    assert cli_main(["--home", str(home), "pr-size", key]) == 0
    return json.loads(capsys.readouterr().out)


def test_lopsided_diff_not_viable(home, capsys):
    out = _pr_size(home, capsys, _setup(home, [925, 25], threshold=800))
    assert out["lines_changed"] == 950
    assert out["exceeds"] is True
    assert out["split_viable"] is False


def test_balanced_diff_viable(home, capsys):
    out = _pr_size(home, capsys, _setup(home, [500, 450], threshold=800))
    assert out["exceeds"] is True
    assert out["split_viable"] is True


def test_single_file_and_pr_open_not_viable(home, capsys):
    out = _pr_size(home, capsys, _setup(home, [950], threshold=800))
    assert out["exceeds"] is True
    assert out["split_viable"] is False


def test_pr_open_not_viable(home, capsys):
    key = _setup(home, [500, 450], threshold=800, pr_open=True)
    out = _pr_size(home, capsys, key)
    assert out["skipped"] == "pr_open"
    assert out["split_viable"] is False


def test_not_exceeding_not_viable(home, capsys):
    out = _pr_size(home, capsys, _setup(home, [50, 50], threshold=800))
    assert out["exceeds"] is False
    assert out["split_viable"] is False


def test_skill_requires_balanced_split():
    for path in (_commands_path("implementing"), _skills_path("implementing")):
        body = _strip_frontmatter(path.read_text())
        assert "split_viable: false" in body
        assert "without a\n   `maestro ask`" in body or "without a `maestro ask`" in body
        assert "⅓" in body
