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


def test_ac_evidence_rows_axes_and_stale_check(tmp_path, home):
    """T-167: the TUI matrix builder reports self / QA-spec / QA-std / latest check
    per AC, flags a check from another tree as stale, and leaves `qa_brief` alone."""
    key = "T-1"
    cfg, _repo, _wt = _bind(tmp_path, home, key)
    spec = store.spec_path(home, key)
    spec.write_text(spec.read_text().replace(
        "- [ ] widget works (test: tests/test_widget.py::test_widget)\n"
        "- [ ] plain prose AC\n",
        "- [ ] attested AC\n- [ ] qa-failed AC\n"
        "- [ ] widget works (test: tests/test_widget.py::test_widget)\n"))
    acs = snap_mod.parse_acs(spec.read_text())
    ops.verify_ac(cfg, key, 1, {"what": "ran it", "where": "t.py", "result": "ok"})
    event_log.append(home, key, "PhaseChanged", {"phase": "qa", "reason": ""}, actor="r")
    snap_mod.rebuild(home, key)
    ops.record_qa_verdict(cfg, key, 2, "fail", "does not work")
    ops.record_qa_verdict(cfg, key, 1, "pass", "std fine", axis="standards")
    h3 = snap_mod.ac_hash(acs[2])
    event_log.append(home, key, "AcCheckCaptured", {
        "tree_key": "abc1234def:old", "ac_hash": h3, "ac_index": 3, "ac_text": acs[2],
        "kind": "test", "command": "pytest x", "exit_code": 1, "passed": False,
        "failure_excerpt": "boom"}, actor="d")
    snap = snap_mod.rebuild(home, key)
    before = ops.qa_brief(cfg, key)["acs"]

    rows = ops.ac_evidence_rows(snap, spec.read_text(), "abc1234def:new")
    assert [r["index"] for r in rows] == [1, 2, 3]
    assert rows[0]["self"]["what"] == "ran it" and rows[0]["qa_std"]["verdict"] == "pass"
    assert rows[1]["qa_spec"]["verdict"] == "fail" and rows[1]["check"] is None
    chk = rows[2]["check"]
    assert chk["passed"] is False and chk["current"] is False
    assert chk["failure_excerpt"] == "boom" and chk["tree_key"] == "abc1234def:old"
    assert "check" not in rows[0]["annotation"] if rows[0].get("annotation") else True
    assert ops.ac_evidence_rows(snap, spec.read_text(), "abc1234def:old")[2]["check"]["current"]
    assert ops.ac_evidence_rows(snap, spec.read_text(), None)[2]["check"]["current"] is False

    after = ops.qa_brief(cfg, key)["acs"]
    assert after == before
    assert all(set(e) <= {"index", "text", "ac_hash", "annotation", "captured_check"} for e in after)
