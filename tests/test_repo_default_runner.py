"""Per-repo default runner: `[repos.<name>] runner` / `runner_model` slot between the
spec `runner:` line and the board-wide `[maestro]` default."""
from __future__ import annotations

import json

from maestro import config as config_mod
from maestro import dispatcher as disp
from maestro import store
from maestro.cli import main as cli_main
from maestro.statemachine import Phase

from tests.conftest import seed_phase


def _cfg(home, *, board="", alpha="", enabled='["claude", "opencode", "pi"]', pi_phases=False):
    text = ('[maestro]\nrepo_path = "/repo/default"\nmin_spawn_interval = 0\n'
            f'runner_enabled = {enabled}\n' + board +
            '\n[repos.alpha]\npath = "/repo/alpha"\nslug = "acme/alpha"\n' + alpha +
            '\n[repos.plain]\npath = "/repo/plain"\nslug = "acme/plain"\n')
    if pi_phases:
        text += '\n[runner.pi]\nphases = ["implementing"]\n'
    (home / "config.toml").write_text(text, encoding="utf-8")
    return config_mod.load(str(home))


def _ticket(home, key, *, repo=None, spec_lines="", phase=Phase.IMPLEMENTING):
    seed_phase(home, key, phase)
    body = f"# {key}\n{('repo: ' + repo) if repo else ''}\n{spec_lines}\n\n## Acceptance criteria\n- [ ] ok\n"
    store.atomic_write(store.spec_path(home, key), body)


def test_repo_runner_used_with_no_spec_line(home):
    cfg = _cfg(home, alpha='runner = "opencode"\nrunner_model = "m-repo"\n')
    _ticket(home, "R-1", repo="alpha")
    assert disp.resolve_runner(cfg, "R-1", "implementing") == ("opencode", "m-repo")


def test_precedence_spec_then_repo_then_board(home):
    cfg = _cfg(home, board='runner = "pi"\nrunner_model = "m-board"\n',
               alpha='runner = "opencode"\nrunner_model = "m-repo"\n')
    _ticket(home, "R-2", repo="alpha", spec_lines="runner: claude\nrunner_model: m-spec")
    assert disp.resolve_runner(cfg, "R-2", "implementing") == ("claude", "m-spec")
    _ticket(home, "R-3", repo="alpha")
    assert disp.resolve_runner(cfg, "R-3", "implementing") == ("opencode", "m-repo")
    _ticket(home, "R-4", repo="plain")
    assert disp.resolve_runner(cfg, "R-4", "implementing") == ("claude", None)  # pi: not eligible


def test_runner_and_model_fall_through_independently(home):
    cfg = _cfg(home, board='runner_model = "m-board"\n', alpha='runner = "opencode"\n')
    _ticket(home, "R-5", repo="alpha")
    assert disp.resolve_runner(cfg, "R-5", "implementing") == ("opencode", "m-board")
    cfg = _cfg(home, board='runner = "opencode"\n', alpha='runner_model = "m-repo"\n')
    assert disp.resolve_runner(cfg, "R-5", "implementing") == ("opencode", "m-repo")


def test_repo_without_key_or_no_repo_resolves_as_before(home):
    cfg = _cfg(home, board='runner = "opencode"\nrunner_model = "m-board"\n',
               alpha='runner = "pi"\n')
    _ticket(home, "R-6", repo="plain")
    _ticket(home, "R-7")
    assert disp.resolve_runner(cfg, "R-6", "implementing") == ("opencode", "m-board")
    assert disp.resolve_runner(cfg, "R-7", "implementing") == ("opencode", "m-board")
    cfg = _cfg(home)
    assert disp.resolve_runner(cfg, "R-6", "implementing") == ("claude", None)


def test_ineligible_phase_falls_back_like_a_spec_choice(home):
    cfg = _cfg(home, alpha='runner = "opencode"\nrunner_model = "m"\n')
    _ticket(home, "R-8", repo="alpha", phase=Phase.QA)
    _ticket(home, "R-9", spec_lines="runner: opencode\nrunner_model: m", phase=Phase.QA)
    assert disp.resolve_runner(cfg, "R-8", "qa") == ("claude", None)
    assert disp.resolve_runner(cfg, "R-9", "qa") == ("claude", None)


def test_repo_runner_outside_runner_enabled_is_gated_like_a_spec_choice(home):
    """resolve_runner returns the name verbatim; the runner_enabled kill switch is the
    caller's gate and treats a repo default and a spec line identically."""
    cfg = _cfg(home, enabled='["claude"]', alpha='runner = "opencode"\n')
    _ticket(home, "R-10", repo="alpha")
    _ticket(home, "R-11", spec_lines="runner: opencode")
    assert disp.resolve_runner(cfg, "R-10", "implementing")[0] == \
        disp.resolve_runner(cfg, "R-11", "implementing")[0] == "opencode"
    assert "opencode" not in cfg.runner_enabled


def test_env_key_reports_repo_runner(home, capsys):
    _cfg(home, alpha='runner = "opencode"\nrunner_model = "m-repo"\n')
    _ticket(home, "R-12", repo="alpha")
    capsys.readouterr()
    assert cli_main(["--home", str(home), "env", "--key", "R-12"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["runner"], printed["runner_model"]) == ("opencode", "m-repo")
