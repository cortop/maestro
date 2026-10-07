"""T-149: per-repo `allowed_paths` -- config knob, the dispatcher's out-of-scope
diff gate (modeled on H4's test_deletion_gate), and the implementing skill."""
from __future__ import annotations

from pathlib import Path

import pytest

from maestro import dispatcher as disp, event_log, ops, repos as repos_mod, snapshot as snap_mod, store
from maestro import config as config_mod
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git, make_origin_and_repo
from test_dispatcher_ac_checks import (
    _PASS_CMD, _advance_to_verifying, _commit_test_file, _run_verifying_to_completion,
    _seed_to_worktree,
)


def _cfg(home, repo, allowed):
    table = {"path": str(repo), "default": True, "test_command": _PASS_CMD}
    if allowed is not None:
        table["allowed_paths"] = allowed
    return Config(home=home, repos={"default": table})


def _asked(home, key):
    return [e["payload"] for e in event_log.read(home, key) if e["type"] == "QuestionAsked"]


def test_config_accepts_both_tables_and_defaults_unrestricted(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.toml").write_text(
        f'[maestro]\nallowed_paths = ["a/**"]\n[repos.r]\npath = "{home}"\nallowed_paths = ["b/**"]\n'
        f'[repos.s]\npath = "{home}"\n')
    cfg = config_mod.load(str(home))
    assert cfg.allowed_paths == ["a/**"]
    assert repos_mod._binding_from_table(cfg, "r", cfg.repos["r"]).allowed_paths == ["b/**"]
    assert repos_mod._binding_from_table(cfg, "s", cfg.repos["s"]).allowed_paths == ["a/**"]
    (home / "config.toml").write_text("")
    assert config_mod.load(str(home)).allowed_paths == []


@pytest.mark.parametrize("bad", ['"src"', '[1]', '"a/**", 2'])
def test_config_malformed_fails_closed(tmp_path, bad):
    home = tmp_path / "h"
    home.mkdir()
    value = bad if bad.startswith(("[", '"src"')) and "," not in bad else f"[{bad}]"
    (home / "config.toml").write_text(f"[maestro]\nallowed_paths = {value}\n")
    with pytest.raises(store.MaestroError, match="allowed_paths"):
        config_mod.load(str(home))
    (home / "config.toml").write_text(f'[repos.r]\npath = "{home}"\nallowed_paths = {value}\n')
    with pytest.raises(store.MaestroError, match="allowed_paths"):
        config_mod.load(str(home))


def test_glob_semantics():
    out = repos_mod.outside_allowed_paths
    assert out(["maestro/providers/cli.py", "maestro/x.py"], ["maestro/**"]) == []
    assert out(["README.md", "tests/a.py"], ["maestro/**", "tests/*.py"]) == ["README.md"]
    assert out(["tests/sub/a.py"], ["tests/*.py"]) == ["tests/sub/a.py"]
    assert out(["x/y/z.md"], []) == []


def test_in_scope_diff_routes_to_qa(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _cfg(home, repo, ["src/**"])
    wt = _seed_to_worktree(cfg, "G-1", acs=["x"])
    _commit_test_file(wt, "src/a/b.py", "x = 1\n")
    _advance_to_verifying(cfg, "G-1")
    _run_verifying_to_completion(cfg, "G-1", DryRunSessions())
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
    assert _asked(home, "G-1") == []


def test_out_of_scope_asks_names_every_file_then_approval_is_tree_keyed(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _cfg(home, repo, ["src/**"])
    wt = _seed_to_worktree(cfg, "G-1", acs=["x"])
    _commit_test_file(wt, "src/a.py", "x = 1\n")
    _commit_test_file(wt, "docs/one.md", "1\n")
    _commit_test_file(wt, "ops/two.sh", "2\n")
    _advance_to_verifying(cfg, "G-1")
    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    assert snap_mod.load(home, "G-1").phase == Phase.AWAITING_HUMAN.value
    (q,) = _asked(home, "G-1")
    assert "docs/one.md" in q["text"] and "ops/two.sh" in q["text"] and "src/a.py" not in q["text"]
    assert all("qa" not in p for _k, p, *_r in sessions.spawned)

    event_log.append(home, "G-1", "QuestionAnswered",
                     {"qid": q["qid"], "answer": "approved"}, actor="human")
    ops.set_phase(cfg, "G-1", Phase.VERIFYING, reason="approved")
    disp.dispatch(cfg, DryRunSessions(), now=1002)
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
    assert len(_asked(home, "G-1")) == 1, "the same tree must never re-ask"

    # A later commit adding a NEW out-of-scope file re-triggers the gate.
    ops.set_phase(cfg, "G-1", Phase.IMPLEMENTING, reason="fix")
    _commit_test_file(wt, "docs/three.md", "3\n")
    ops.set_phase(cfg, "G-1", Phase.QA, reason="pr opened")
    _run_verifying_to_completion(cfg, "G-1", DryRunSessions())
    assert snap_mod.load(home, "G-1").phase == Phase.AWAITING_HUMAN.value
    qs = _asked(home, "G-1")
    assert len(qs) == 2 and qs[1]["qid"] != q["qid"] and "docs/three.md" in qs[1]["text"]


def test_unset_means_no_restriction(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _cfg(home, repo, None)
    wt = _seed_to_worktree(cfg, "G-1", acs=["x"])
    _commit_test_file(wt, "anywhere/f.txt", "x\n")
    _advance_to_verifying(cfg, "G-1")
    _run_verifying_to_completion(cfg, "G-1", DryRunSessions())
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value


def test_implementing_skill_tells_reconciler_to_read_allowed_paths():
    text = (Path(__file__).parent.parent / ".claude/commands/maestro-reconcile-implementing.md").read_text()
    assert "allowed_paths" in text and "maestro env --key" in text and "maestro ask" in text
