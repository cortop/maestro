"""`qa_skill`: a per-repo review skill the `qa` reconcile step runs on the diff.

Real surface: `config.load()` over a temp home, a real `dispatch()` sweep with
`DryRunSessions` (the `claude -p` spawn is the only mock), and the real
`maestro env --key` CLI the QA skill file reads the knob from.
"""
from __future__ import annotations

import json

import pytest

from maestro import cli, config as config_mod, dispatcher as disp, event_log
from maestro import repos as repos_mod, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _write_config(home, *, board="", repos):
    lines = ["[maestro]", 'repo_path = "/repo/default"', "min_spawn_interval = 0", board]
    for name, table in repos.items():
        lines.append(f"\n[repos.{name}]")
        lines += [f'{k} = "{v}"' for k, v in table.items()]
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_mod.load(str(home))


def _seed(home, key, phase, *, repo):
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key),
                      "repo": repo}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def _skill_rules_by_key(cfg):
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    return {s[0]: {r for r in s[6] if r.startswith("Skill")} for s in sessions.spawned}


# --- config ------------------------------------------------------------------

def test_repo_override_wins_and_unset_repo_inherits_board_wide(home):
    cfg = _write_config(home, board='qa_skill = "/board-review"', repos={
        "web": {"path": "/repo/web", "qa_skill": "/ui-review"},
        "api": {"path": "/repo/api"},
    })
    assert repos_mod._binding_from_table(cfg, "web", cfg.repos["web"]).qa_skill == \
        "/ui-review"
    assert repos_mod._binding_from_table(cfg, "api", cfg.repos["api"]).qa_skill == \
        "/board-review"


def test_plugin_namespaced_skill_is_accepted(home):
    cfg = _write_config(home, repos={"api": {"path": "/repo/api",
                                             "qa_skill": "/my-plugin:py-review"}})
    assert cfg.repos["api"]["qa_skill"] == "/my-plugin:py-review"


@pytest.mark.parametrize("bad", ["py-review", "/a:", "/:b", "/a:b:c", "/a: b"])
def test_malformed_qa_skill_fails_load_closed(home, bad):
    with pytest.raises(store.MaestroError, match="qa_skill"):
        _write_config(home, repos={"api": {"path": "/repo/api", "qa_skill": bad}})


# --- spawn grant -------------------------------------------------------------

def test_qa_spawn_is_granted_its_repos_qa_skill_only(home):
    cfg = _write_config(home, repos={
        "web": {"path": "/repo/web", "qa_skill": "/ui-review"},
        "api": {"path": "/repo/api"},
    })
    _seed(home, "W-1", Phase.QA, repo="web")
    _seed(home, "A-1", Phase.QA, repo="api")
    rules = _skill_rules_by_key(cfg)
    assert rules["W-1"] == {"Skill(maestro-reconcile-qa)", "Skill(ui-review)"}
    assert rules["A-1"] == {"Skill(maestro-reconcile-qa)"}


def test_non_qa_phase_is_never_granted_the_qa_skill(home):
    cfg = _write_config(home, repos={"web": {"path": "/repo/web",
                                             "qa_skill": "/ui-review"}})
    _seed(home, "W-1", Phase.IMPLEMENTING, repo="web")
    assert _skill_rules_by_key(cfg)["W-1"] == {"Skill(maestro-reconcile-implementing)"}


# --- maestro env --key -----------------------------------------------------------

def test_env_key_reports_the_resolved_qa_skill(home, capsys):
    _write_config(home, repos={"web": {"path": "/repo/web", "qa_skill": "/ui-review"},
                               "api": {"path": "/repo/api"}})
    _seed(home, "W-1", Phase.QA, repo="web")
    _seed(home, "A-1", Phase.QA, repo="api")
    capsys.readouterr()
    assert cli.main(["--home", str(home), "env", "--key", "W-1"]) == 0
    assert json.loads(capsys.readouterr().out)["qa_skill"] == "/ui-review"
    assert cli.main(["--home", str(home), "env", "--key", "A-1"]) == 0
    assert json.loads(capsys.readouterr().out)["qa_skill"] is None
