"""T-98: `[repos.<name>] test_selector` -- a per-repo invocation template
orthogonal to `language`'s extraction axis. Driven through a real
`dispatch()` sweep exactly like `test_dispatcher_ac_checks_lang.py` (T-84):
`language = "go"` still selects the added-test EXTRACTOR (`testlang.GO.
added_re`), while `test_selector` overrides ONLY how the named test is
invoked -- proving the two axes are genuinely independent.

The `bzl test ...`-shaped template is rendered against a stand-in
`test_command` (no real Bazel install needed, matching the ticket's own
Notes) -- what's under test is maestro's OWN selector composition +
subprocess/exit-code wiring, never Bazel's actual semantics.
"""
from __future__ import annotations

import sys

from maestro import dispatcher as disp, event_log, ops, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git, make_origin_and_repo
from test_dispatcher_ac_checks import (
    _advance_to_verifying, _commit_test_file, _run_verifying_to_completion, _seed_to_worktree,
)

_PASS_CMD = f'{sys.executable} -c "import sys; sys.exit(0)"'
_BAZEL_SHAPED_SELECTOR = '{test_command} --dir="{dir}" --test_filter="^({names})$"'


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


def _selector_config(home, repo, *, test_selector):
    """`language = "go"` (extraction) + a custom `test_selector` (invocation)
    on the SAME `[repos.default]` table -- proves the two axes compose
    independently. Bypasses `config.load()`'s TOML parsing/validation on
    purpose, exactly like `test_repo_test_command.py`/T-84's own
    `_lang_config` -- the validation path itself is covered separately in
    `tests/test_repos.py`."""
    return Config(home=home, repos={
        "default": {"path": str(repo), "default": True, "test_command": _PASS_CMD,
                    "language": "go", "test_selector": test_selector},
    })


# ---------------------------------------------------------------------------
# AC3: a `test_selector` composes AND runs the bazel-shaped command; the
# added-by-diff check still goes through Go's OWN extractor.
# ---------------------------------------------------------------------------

def test_custom_selector_composes_and_runs_the_bazel_shaped_command(tmp_path, home):
    repo = _go_repo(tmp_path)
    cfg = _selector_config(home, repo, test_selector=_BAZEL_SHAPED_SELECTOR)
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
    command = captured["payload"]["command"]
    # The BAZEL-shaped template, not go's own `-run '^(...)$'` selector.
    assert '--dir="internal/widget"' in command
    assert '--test_filter="^(TestWidget)$"' in command
    assert "-run" not in command


def test_custom_selector_still_catches_a_test_not_added_by_the_diff(tmp_path, home):
    """AC4: the T-55 false-attestation class stays closed on the custom-selector
    path too -- a green suite whose diff added a DIFFERENT test than the one
    named still fails, with the same excerpt."""
    repo = _go_repo(tmp_path)
    cfg = _selector_config(home, repo, test_selector=_BAZEL_SHAPED_SELECTOR)
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
# AC7: the bare-path "no test added" fallback composes through the resolved
# selector too (never the hardcoded pytest `f"{test_command} {path}"` shape).
# ---------------------------------------------------------------------------

def test_bare_path_no_test_added_records_a_selector_composed_command(tmp_path, home):
    repo = _go_repo(tmp_path)
    cfg = _selector_config(home, repo, test_selector=_BAZEL_SHAPED_SELECTOR)
    wt = _seed_to_worktree(
        cfg, "G-1", acs=["widget adds (test: internal/widget/widget_test.go)"])
    _commit_test_file(wt, "internal/widget/widget_test.go", "// placeholder, no test yet\n")
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    snap = snap_mod.load(home, "G-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    captured = [e for e in event_log.read(home, "G-1") if e["type"] == "AcCheckCaptured"][0]
    assert captured["payload"]["passed"] is False
    assert "no test added by this diff" in captured["payload"]["failure_excerpt"]
    command = captured["payload"]["command"]
    # Composed through the BAZEL-shaped template -- never the pytest shape.
    assert command != f"{_PASS_CMD} internal/widget/widget_test.go"
    assert '--dir="internal/widget"' in command


# ---------------------------------------------------------------------------
# AC5: a free-text (TypeScript) test name containing a shell-unsafe character
# is refused, not interpolated raw into the `test_selector` command line.
# ---------------------------------------------------------------------------

def test_custom_selector_refuses_an_unsafe_test_name(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path, name="tsapp")
    cfg = Config(home=home, repos={
        "tsapp": {"path": str(repo), "base_branch": "main", "branch_prefix": "maestro/",
                 "test_command": _PASS_CMD, "language": "typescript",
                 "test_selector": '{test_command} -t "{names}"'},
    })
    wt = store.worktree_path(home, "G-1")
    ac_lines = "- [ ] widget renders (test: src/widget.spec.ts)"
    store.atomic_write(store.spec_path(home, "G-1"),
                       f"# G-1: t\npriority: 2\nrepo: tsapp\n\n## Intent\nx\n\n"
                       f"## Acceptance criteria\n{ac_lines}\n")
    event_log.append(home, "G-1", "TicketCreated",
                     {"title": "G-1", "source": "test", "repo": "tsapp"}, actor="d")
    snap_mod.rebuild(home, "G-1")
    ops.set_phase(cfg, "G-1", Phase.READY, reason="approved")
    ops.worktree_ensure(cfg, "G-1")
    (wt / "src").mkdir()
    (wt / "src" / "widget.spec.ts").write_text(
        'it("does $(rm -rf /) stuff", () => {\n  expect(1).toBe(1);\n});\n')
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "add unsafe-named test", cwd=wt)

    result = ops.run_ac_checks(cfg, "G-1", wt)

    assert result["all_passed"] is False
    captured = [e for e in event_log.read(home, "G-1") if e["type"] == "AcCheckCaptured"][0]
    assert captured["payload"]["passed"] is False
    assert "unsafe" in captured["payload"]["failure_excerpt"]
    # Never actually shelled out with the raw name substituted.
    assert "$(rm -rf /)" not in captured["payload"]["command"]
