"""T-79 AC6: `maestro qa-brief` carries each annotated AC's captured check
result, so QA can audit test-vs-AC fidelity -- without re-deriving it from the
raw diff every round. An unannotated AC is unaffected (no `annotation`/
`captured_check` key at all, matching `test_qa_brief.py`'s existing
assertions byte-for-byte).
"""
from __future__ import annotations

from maestro import event_log, ops, snapshot as snap_mod, store
from maestro.config import Config

from conftest import git, make_origin_and_repo


def _bind(tmp_path, home, key, *, test_command=None):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo), test_command=test_command)
    store.atomic_write(
        store.spec_path(home, key),
        f"# {key}: t\npriority: 2\n\n## Intent\nx\n\n## Acceptance criteria\n"
        "- [ ] widget works (test: tests/test_widget.py::test_widget)\n"
        "- [ ] plain prose AC\n")
    event_log.append(home, key, "TicketCreated", {"title": "t", "source": "test"}, actor="d")
    snap_mod.rebuild(home, key)
    wt = store.worktree_path(home, key)
    wt.symlink_to(repo)
    return cfg, repo, wt


def test_brief_carries_the_annotation_for_an_annotated_ac(tmp_path, home):
    cfg, _repo, _wt = _bind(tmp_path, home, "T-1")
    brief = ops.qa_brief(cfg, "T-1")

    ann_entry = brief["acs"][0]
    assert ann_entry["annotation"] == {"kind": "test", "raw": "tests/test_widget.py::test_widget"}
    assert "captured_check" not in ann_entry  # nothing captured yet

    plain_entry = brief["acs"][1]
    assert "annotation" not in plain_entry
    assert "captured_check" not in plain_entry


def test_brief_carries_the_captured_check_once_the_verifying_stage_ran_it(tmp_path, home):
    cfg, repo, wt = _bind(tmp_path, home, "T-1", test_command="true")
    git("checkout", "-q", "-b", "maestro/T-1", cwd=repo)
    (repo / "tests" / "test_widget.py").parent.mkdir(exist_ok=True)
    (repo / "tests" / "test_widget.py").write_text("def test_widget():\n    assert True\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add test", cwd=repo)

    ops.run_ac_checks(cfg, "T-1", wt)

    brief = ops.qa_brief(cfg, "T-1")
    ann_entry = brief["acs"][0]
    assert ann_entry["captured_check"]["passed"] is True
    assert ann_entry["captured_check"]["kind"] == "test"


def test_brief_still_requires_at_least_one_ac_and_appends_no_event(tmp_path, home):
    cfg, _repo, _wt = _bind(tmp_path, home, "T-1")
    before = len(event_log.read(home, "T-1"))
    ops.qa_brief(cfg, "T-1")
    assert len(event_log.read(home, "T-1")) == before
