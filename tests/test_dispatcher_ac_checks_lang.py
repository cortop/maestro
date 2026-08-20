"""T-84: per-language `test:` extraction/selector formatting and the H4
test-deletion gate, generalized beyond python/pytest -- Go and TypeScript
end-to-end, driven through a real `dispatch()` sweep exactly like
`test_dispatcher_ac_checks.py` (T-79). `binding.language` selects the table
entry (`maestro/testlang.py`) both `ops.run_ac_checks` and `dispatcher`'s H4
gate resolve through -- see AC1-AC5 of T-84's spec.

The TypeScript cases stand in for `jest` (not installed in this sandbox)
with `_PASS_CMD`, exactly the way `test_dispatcher_ac_checks.py` already
stands in for a real suite with a trivial python one-liner for its `check:`
tests -- what's under test is maestro's OWN diff-scan + selector-construction
+ subprocess/exit-code wiring, never jest's actual assertion semantics.
Go runs a REAL `go test` (the toolchain is present here).
"""
from __future__ import annotations

import shutil
import sys

import pytest

from maestro import claims, dispatcher as disp, event_log, ops, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git, make_origin_and_repo
from test_dispatcher_ac_checks import (
    _advance_to_verifying, _commit_test_file, _run_verifying_to_completion, _seed_to_worktree,
    _wait_until_dead, _PASS_CMD,
)

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not installed")


def _lang_config(home, repo, *, language, test_command):
    return Config(home=home, repos={
        "default": {"path": str(repo), "default": True, "test_command": test_command,
                    "language": language},
    })


def _go_repo(tmp_path):
    _origin, repo = make_origin_and_repo(tmp_path)
    (repo / "go.mod").write_text("module widgetmod\n\ngo 1.21\n")
    (repo / "internal" / "widget").mkdir(parents=True)
    (repo / "internal" / "widget" / "widget.go").write_text(
        "package widget\n\nfunc Add(a, b int) int { return a + b }\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "go module base", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    return repo


# ---------------------------------------------------------------------------
# AC2/AC4: Go `test:` -- added-by-diff presence + a go-test-shaped selector.
# ---------------------------------------------------------------------------

def test_go_named_test_added_by_diff_admits_qa_with_a_go_shaped_selector(tmp_path, home):
    repo = _go_repo(tmp_path)
    cfg = _lang_config(home, repo, language="go", test_command="go test ./...")
    wt = _seed_to_worktree(
        cfg, "G-1",
        acs=["widget adds (test: internal/widget/widget_test.go::TestWidget)"])
    _commit_test_file(
        wt, "internal/widget/widget_test.go",
        'package widget\n\nimport "testing"\n\n'
        'func TestWidget(t *testing.T) {\n\tif Add(1, 2) != 3 {\n\t\tt.Fatal("bad")\n\t}\n}\n')
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
    captured = [e for e in event_log.read(home, "G-1") if e["type"] == "AcCheckCaptured"][0]
    assert captured["payload"]["passed"] is True
    assert captured["payload"]["kind"] == "test"
    command = captured["payload"]["command"]
    assert "go test" in command and "-run" in command and "TestWidget" in command
    assert "internal/widget" in command


def test_go_named_test_not_added_by_diff_fails_even_with_a_green_suite(tmp_path, home):
    """The T-55 false-attestation class, generalized to Go: the suite is
    green, but the branch's diff adds a DIFFERENT test than the one named --
    a stale/unrelated test of the same file must not satisfy the gate."""
    repo = _go_repo(tmp_path)
    cfg = _lang_config(home, repo, language="go", test_command="go test ./...")
    wt = _seed_to_worktree(
        cfg, "G-1",
        acs=["widget adds (test: internal/widget/widget_test.go::TestWidget)"])
    _commit_test_file(
        wt, "internal/widget/widget_test.go",
        'package widget\n\nimport "testing"\n\n'
        'func TestSomethingElse(t *testing.T) {\n\tif Add(1, 1) != 2 {\n\t\tt.Fatal("bad")\n\t}\n}\n')
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    snap = snap_mod.load(home, "G-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    captured = [e for e in event_log.read(home, "G-1") if e["type"] == "AcCheckCaptured"][0]
    assert captured["payload"]["passed"] is False
    assert "not found among tests added by this diff" in captured["payload"]["failure_excerpt"]
    assert all("qa" not in p for _k, p, *_r in sessions.spawned)


# ---------------------------------------------------------------------------
# AC3/AC4: TypeScript `test:` -- added-by-diff presence + a jest-shaped
# selector (`-t "<name>"`).
# ---------------------------------------------------------------------------

def test_typescript_named_test_added_by_diff_admits_qa_with_a_jest_shaped_selector(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _lang_config(home, repo, language="typescript", test_command=_PASS_CMD)
    wt = _seed_to_worktree(
        cfg, "G-1",
        acs=['widget renders (test: src/widget.spec.ts::renders the widget)'])
    _commit_test_file(
        wt, "src/widget.spec.ts",
        'it("renders the widget", () => {\n  expect(1 + 1).toBe(2);\n});\n')
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
    captured = [e for e in event_log.read(home, "G-1") if e["type"] == "AcCheckCaptured"][0]
    assert captured["payload"]["passed"] is True
    assert captured["payload"]["kind"] == "test"
    command = captured["payload"]["command"]
    assert "-t" in command and "src/widget.spec.ts" in command and "renders" in command


def test_typescript_named_test_not_added_by_diff_fails_even_with_a_green_suite(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _lang_config(home, repo, language="typescript", test_command=_PASS_CMD)
    wt = _seed_to_worktree(
        cfg, "G-1",
        acs=['widget renders (test: src/widget.spec.ts::renders the widget)'])
    _commit_test_file(
        wt, "src/widget.spec.ts",
        'it("does something else", () => {\n  expect(1 + 1).toBe(2);\n});\n')
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    snap = snap_mod.load(home, "G-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    captured = [e for e in event_log.read(home, "G-1") if e["type"] == "AcCheckCaptured"][0]
    assert captured["payload"]["passed"] is False
    assert "not found among tests added by this diff" in captured["payload"]["failure_excerpt"]
    assert all("qa" not in p for _k, p, *_r in sessions.spawned)


# ---------------------------------------------------------------------------
# AC5: H4 test-deletion gate sees deletions outside `tests/` -- a co-located
# `*_test.go` and a `*.spec.ts` under `__tests__/`, both over a real sweep.
# ---------------------------------------------------------------------------

def test_h4_catches_a_deleted_go_test_beside_its_source(tmp_path, home):
    repo = _go_repo(tmp_path)
    (repo / "internal" / "widget" / "widget_test.go").write_text(
        'package widget\n\nimport "testing"\n\n'
        'func TestKeep(t *testing.T) {\n\tif Add(1, 1) != 2 {\n\t\tt.Fatal("bad")\n\t}\n}\n')
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add existing test", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    cfg = _lang_config(home, repo, language="go", test_command="go test ./...")
    wt = _seed_to_worktree(cfg, "G-1", acs=["unrelated change"])
    git("rm", "-q", "internal/widget/widget_test.go", cwd=wt)
    git("commit", "-q", "-m", "tidy tests", cwd=wt)
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    snap = snap_mod.load(home, "G-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    text = next(e["payload"]["text"] for e in event_log.read(home, "G-1")
               if e["type"] == "QuestionAsked")
    assert "TestKeep" in text
    assert all("qa" not in p for _k, p, *_r in sessions.spawned)


def test_h4_catches_a_deleted_typescript_spec_under_dunder_tests(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    (repo / "__tests__").mkdir()
    (repo / "__tests__" / "widget.spec.ts").write_text(
        'it("keeps working", () => {\n  expect(1).toBe(1);\n});\n')
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add existing test", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    cfg = _lang_config(home, repo, language="typescript", test_command=_PASS_CMD)
    wt = _seed_to_worktree(cfg, "G-1", acs=["unrelated change"])
    git("rm", "-q", "__tests__/widget.spec.ts", cwd=wt)
    git("commit", "-q", "-m", "tidy tests", cwd=wt)
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    snap = snap_mod.load(home, "G-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    text = next(e["payload"]["text"] for e in event_log.read(home, "G-1")
               if e["type"] == "QuestionAsked")
    assert "keeps working" in text
    assert all("qa" not in p for _k, p, *_r in sessions.spawned)


def test_h4_an_answered_sign_off_for_the_same_tree_passes(tmp_path, home):
    repo = _go_repo(tmp_path)
    (repo / "internal" / "widget" / "widget_test.go").write_text(
        'package widget\n\nimport "testing"\n\n'
        'func TestKeep(t *testing.T) {\n\tif Add(1, 1) != 2 {\n\t\tt.Fatal("bad")\n\t}\n}\n')
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add existing test", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    cfg = _lang_config(home, repo, language="go", test_command="go test ./...")
    wt = _seed_to_worktree(cfg, "G-1", acs=["unrelated change"])
    git("rm", "-q", "internal/widget/widget_test.go", cwd=wt)
    git("commit", "-q", "-m", "tidy tests", cwd=wt)
    _advance_to_verifying(cfg, "G-1")

    _run_verifying_to_completion(cfg, "G-1", DryRunSessions())
    assert snap_mod.load(home, "G-1").phase == Phase.AWAITING_HUMAN.value
    qid = next(e["payload"]["qid"] for e in event_log.read(home, "G-1")
              if e["type"] == "QuestionAsked")
    event_log.append(home, "G-1", "QuestionAnswered",
                     {"qid": qid, "answer": "approved -- that test covered removed code"},
                     actor="human")
    ops.set_phase(cfg, "G-1", Phase.VERIFYING, reason="deletion approved")

    # The tree is UNCHANGED, so `sync_test_runs` reuses the already-captured
    # TestRunCaptured record and routes synchronously within this one sweep --
    # no new async subprocess/claim, unlike a fresh tree state.
    disp.dispatch(cfg, DryRunSessions(), now=1002)
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
    qids = [e["payload"]["qid"] for e in event_log.read(home, "G-1") if e["type"] == "QuestionAsked"]
    assert len(qids) == 1, "the same tree must never re-ask"


def test_h4_knob_off_disables_the_gate(tmp_path, home):
    repo = _go_repo(tmp_path)
    (repo / "internal" / "widget" / "widget_test.go").write_text(
        'package widget\n\nimport "testing"\n\n'
        'func TestKeep(t *testing.T) {\n\tif Add(1, 1) != 2 {\n\t\tt.Fatal("bad")\n\t}\n}\n')
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add existing test", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    cfg = _lang_config(home, repo, language="go", test_command="go test ./...")
    cfg.test_deletion_gate = False
    wt = _seed_to_worktree(cfg, "G-1", acs=["unrelated change"])
    git("rm", "-q", "internal/widget/widget_test.go", cwd=wt)
    git("commit", "-q", "-m", "tidy tests", cwd=wt)
    _advance_to_verifying(cfg, "G-1")

    _run_verifying_to_completion(cfg, "G-1", DryRunSessions())
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value


def test_h4_a_rename_is_not_a_deletion(tmp_path, home):
    repo = _go_repo(tmp_path)
    (repo / "internal" / "widget" / "widget_test.go").write_text(
        'package widget\n\nimport "testing"\n\n'
        'func TestKeep(t *testing.T) {\n\tif Add(1, 1) != 2 {\n\t\tt.Fatal("bad")\n\t}\n}\n')
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add existing test", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    cfg = _lang_config(home, repo, language="go", test_command="go test ./...")
    wt = _seed_to_worktree(cfg, "G-1", acs=["unrelated change"])
    moved = (wt / "internal" / "widget" / "widget_test.go").read_text()
    git("rm", "-q", "internal/widget/widget_test.go", cwd=wt)
    (wt / "internal" / "widget" / "widget_moved_test.go").write_text(moved)
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "move test", cwd=wt)
    _advance_to_verifying(cfg, "G-1")

    _run_verifying_to_completion(cfg, "G-1", DryRunSessions())
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
