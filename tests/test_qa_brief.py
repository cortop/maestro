"""`maestro qa-brief` — the deterministic Implementer->QA hand-off packet.

The QA sub-agent used to be briefed with diff text the implementer re-typed into
the prompt; these tests pin the packet to a CLI result instead, over real git and
the real CLI (never a mocked `git diff`).
"""
import json
import subprocess

import pytest

from maestro import cli, event_log, ops, snapshot as snap_mod, store
from maestro.config import Config

from conftest import git, make_origin_and_repo

SPEC_TEMPLATE = """\
# {key}: Test ticket

approval_tier: 1
priority: 2

## Intent
do the thing

## Acceptance criteria
{acs}
"""


def _bind(tmp_path, home, key, *, base_branch="main"):
    """A home whose ticket resolves to a real git clone, with a worktree on disk."""
    _origin, repo = make_origin_and_repo(tmp_path, base_branch=base_branch)
    cfg = Config(home=home, repo_path=str(repo))
    cfg.repos = {"repo": {"path": str(repo), "base_branch": base_branch, "default": True}}
    store.atomic_write(store.spec_path(home, key),
                       SPEC_TEMPLATE.format(key=key, acs="- [ ] build the widget\n- [ ] document it"))
    event_log.append(home, key, "TicketCreated", {"title": "Test ticket", "source": "test"}, actor="d")
    snap_mod.rebuild(home, key)
    return cfg, repo


def test_brief_carries_every_ac_with_its_hash(tmp_path, home):
    cfg, _repo = _bind(tmp_path, home, "T-1")
    brief = ops.qa_brief(cfg, "T-1")

    assert [a["index"] for a in brief["acs"]] == [1, 2]
    assert [a["text"] for a in brief["acs"]] == ["build the widget", "document it"]
    # Same hashing verify-ac/qa-verdict key their events by, so a QA agent can
    # brief and verdict against the same identity without re-deriving it.
    spec_text = store.spec_path(home, "T-1").read_text(encoding="utf-8")
    expected = [snap_mod.ac_hash(t) for t in snap_mod.parse_acs(spec_text)]
    assert [a["ac_hash"] for a in brief["acs"]] == expected


def test_brief_contains_the_real_uncommitted_diff(tmp_path, home):
    """The whole point: the packet carries the actual change, not a description."""
    cfg, repo = _bind(tmp_path, home, "T-1")
    (repo / "widget.py").write_text("def build():\n    return 'widget'\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add widget", cwd=repo)

    brief = ops.qa_brief(cfg, "T-1")
    assert brief["diff_empty"] is False
    assert "widget.py" in brief["diff"]
    assert "def build():" in brief["diff"]
    assert brief["base_ref"] == "origin/main"


def test_brief_reports_empty_diff_rather_than_raising(tmp_path, home):
    """An implementer that has written nothing yet must still get a packet — an
    empty diff is a legitimate QA input (verdict: fail), not a failed step."""
    cfg, _repo = _bind(tmp_path, home, "T-1")
    brief = ops.qa_brief(cfg, "T-1")
    assert brief["diff_empty"] is True
    assert brief["diff"].strip() == ""


def test_brief_honors_a_non_main_base_branch(tmp_path, home):
    cfg, repo = _bind(tmp_path, home, "T-1", base_branch="trunk")
    (repo / "widget.py").write_text("x = 1\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add widget", cwd=repo)

    brief = ops.qa_brief(cfg, "T-1")
    assert brief["base_ref"] == "origin/trunk"
    assert "widget.py" in brief["diff"]


def test_brief_excludes_changes_the_base_gained_after_branching(tmp_path, home):
    """Three-dot, not two-dot. Observed on a real dogfood worktree: the two-dot
    form dragged 72,573 bytes of unrelated main-advancement into a one-commit
    branch's 10,876-byte diff. QA must judge only what THIS branch introduced."""
    cfg, repo = _bind(tmp_path, home, "T-1")
    # This ticket's work, on its own branch.
    git("checkout", "-q", "-b", "maestro/T-1", cwd=repo)
    (repo / "widget.py").write_text("def build():\n    return 'widget'\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "T-1: add widget", cwd=repo)
    # Meanwhile someone else lands an unrelated change on the base.
    git("checkout", "-q", "main", cwd=repo)
    (repo / "unrelated.py").write_text("# nothing to do with T-1\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "unrelated work", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    git("checkout", "-q", "maestro/T-1", cwd=repo)

    brief = ops.qa_brief(cfg, "T-1")
    assert "widget.py" in brief["diff"]
    assert "unrelated.py" not in brief["diff"], "base-advancement leaked into the QA packet"
    assert brief["commits_ahead"] == 1


def test_brief_includes_uncommitted_work(tmp_path, home):
    """The case a commit-to-commit diff would silently drop: mid-`implementing`
    the agent has edited files but not committed. QA must still see them."""
    cfg, repo = _bind(tmp_path, home, "T-1")
    git("checkout", "-q", "-b", "maestro/T-1", cwd=repo)
    (repo / "widget.py").write_text("def build():\n    return 'widget'\n")  # never committed

    brief = ops.qa_brief(cfg, "T-1")
    assert brief["diff_empty"] is False, "uncommitted work vanished from the QA packet"
    assert "widget.py" in brief["diff"]
    assert brief["commits_ahead"] == 0


def test_brief_reports_committed_and_uncommitted_together(tmp_path, home):
    cfg, repo = _bind(tmp_path, home, "T-1")
    git("checkout", "-q", "-b", "maestro/T-1", cwd=repo)
    (repo / "widget.py").write_text("def build():\n    return 'widget'\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "T-1: add widget", cwd=repo)
    (repo / "docs.md").write_text("# widget\n")  # staged nowhere, still pending

    brief = ops.qa_brief(cfg, "T-1")
    assert "widget.py" in brief["diff"] and "docs.md" in brief["diff"]
    assert brief["commits_ahead"] == 1


def test_brief_appends_no_event(tmp_path, home):
    """Read-only: safe to call on every QA round and safe to retry."""
    cfg, _repo = _bind(tmp_path, home, "T-1")
    before = len(event_log.read(home, "T-1"))
    ops.qa_brief(cfg, "T-1")
    ops.qa_brief(cfg, "T-1")
    assert len(event_log.read(home, "T-1")) == before


def test_brief_twice_leaves_worktree_and_index_unmodified(tmp_path, home):
    """The untracked-file path (`git diff --no-index`) must never fall back to
    `git add -N` or any other staging trick -- that would make a read-only
    briefing call mutate the index a later `git add -A` (step 5) then commits
    unintentionally. Calling it twice, with both a committed and an untracked
    file in play, must leave `git status --porcelain` byte-identical."""
    cfg, repo = _bind(tmp_path, home, "T-1")
    (repo / "widget.py").write_text("def build():\n    return 'widget'\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add widget", cwd=repo)
    (repo / "test_widget.py").write_text("def test_build():\n    assert build()\n")  # untracked

    status_before = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                    check=True, capture_output=True, text=True).stdout

    brief = ops.qa_brief(cfg, "T-1")
    ops.qa_brief(cfg, "T-1")  # idempotent retry, same QA round

    status_after = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                   check=True, capture_output=True, text=True).stdout
    assert status_after == status_before, "qa-brief mutated the worktree/index"
    assert "test_widget.py" in status_before, "test setup lost its untracked file"
    assert "test_widget.py" in brief["diff"], "untracked file never reached the QA packet"


def test_brief_rejects_a_spec_with_no_acs(tmp_path, home):
    cfg, _repo = _bind(tmp_path, home, "T-1")
    store.atomic_write(store.spec_path(home, "T-1"),
                       SPEC_TEMPLATE.format(key="T-1", acs="(none yet)"))
    with pytest.raises(store.MaestroError, match="no acceptance criteria"):
        ops.qa_brief(cfg, "T-1")


def test_qa_brief_via_real_cli(tmp_path, home, capsys):
    """CLI-driven, per the QA convention: exercise the actual `maestro` surface."""
    cfg, repo = _bind(tmp_path, home, "T-1")
    (repo / "widget.py").write_text("def build():\n    return 'widget'\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add widget", cwd=repo)

    # The real CLI resolves its repo binding from the home's config.toml, so write
    # one rather than hand it a Config the way the ops-level tests do.
    store.atomic_write(home / "config.toml",
                       f'[maestro]\nrepo_path = "{repo}"\n')
    rc = cli.main(["--home", str(home), "qa-brief", "T-1"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out["key"] == "T-1"
    assert len(out["acs"]) == 2
    assert "widget.py" in out["diff"]
    assert out["diff_empty"] is False


def test_qa_brief_is_granted_to_reconcilers(home):
    """It is useless if a spawned reconciler is not allowed to run it."""
    grants = cli._reconciler_tool_grants(Config(home=home))
    assert any("qa-brief" in g for g in grants)
