"""T-144: scrub secrets maestro knows a reconciler -- or an agent-written
test/check-command subprocess it goes on to run -- doesn't need, out of every
spawned/dispatcher-run child env: another repo's `token_env`, the configured
tracker's key, and any `[maestro] scrub_env` entry. `GH_TOKEN` and every
runner-auth variable (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, an
opencode/pi provider key) always pass through unchanged.

Every test drives real code (`config.scrubbed_env`, real `dispatch()` sweeps,
real `ops.capture_tests`/`run_ac_checks`, real git repos). The only mocks are
the external boundaries the rest of the suite already treats that way: the
runner-launch `subprocess.Popen` call (never `subprocess.run` -- git/gh still
run for real), and a throwaway `gt` shell stub that dumps its own env to a
file so the test can inspect exactly what a real subprocess saw.
"""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

from maestro import claims, config as config_mod, dispatcher as disp, event_log, ops
from maestro import snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.config import Config
from maestro.sessions import ClaudeCliSessions, DryRunSessions, OpencodeCliSessions, PiCliSessions
from maestro.statemachine import Phase

from conftest import make_origin_and_repo
from test_dispatcher_ac_checks import _advance_to_verifying, _seed_to_worktree
from test_gh_credentials import _seed, _write_config as _write_toml_config


def _wait_until_dead(pid, *, timeout=15.0):
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not claims.pid_alive(pid):
            return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} still alive after {timeout}s")


# --- config.scrubbed_env: the shared helper every site below builds on -------

def test_scrubbed_env_drops_repo_token_envs_tracker_key_and_scrub_env_patterns(home, monkeypatch):
    monkeypatch.setenv("GH_TOKEN_ALPHA", "tok-alpha")
    monkeypatch.setenv("GH_TOKEN_BETA", "tok-beta")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-secret")
    monkeypatch.setenv("MY_OTHER_SECRET", "shh")
    monkeypatch.setenv("GLOBBED_SECRET_X", "shh2")
    cfg = _write_toml_config(home, repos={
        "alpha": {"path": "/repo/alpha", "token_env": "GH_TOKEN_ALPHA"},
        "beta": {"path": "/repo/beta", "token_env": "GH_TOKEN_BETA"},
    })
    cfg.providers["tracker"] = "jira"
    cfg.scrub_env = ["MY_OTHER_SECRET", "GLOBBED_SECRET_*"]

    env = config_mod.scrubbed_env(cfg, base_env=dict(os.environ))

    assert "GH_TOKEN_ALPHA" not in env
    assert "GH_TOKEN_BETA" not in env
    assert "JIRA_API_TOKEN" not in env
    assert "MY_OTHER_SECRET" not in env
    assert "GLOBBED_SECRET_X" not in env
    assert env["HOME"] == os.environ["HOME"]  # unrelated ambient vars pass through


def test_scrubbed_env_never_drops_gh_token_even_if_named_explicitly(home, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ambient-gh")
    cfg = _write_toml_config(home, repos={"alpha": {"path": "/repo/alpha", "token_env": "GH_TOKEN"}})
    cfg.scrub_env = ["GH_TOKEN"]

    env = config_mod.scrubbed_env(cfg, base_env=dict(os.environ))

    assert env["GH_TOKEN"] == "ambient-gh"


def test_scrubbed_env_keeps_runner_auth_vars_untouched(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "o-token")
    monkeypatch.setenv("MY_PI_KEY", "pi-key")
    cfg = config_mod.load(str(home))

    env = config_mod.scrubbed_env(cfg, base_env=dict(os.environ))

    assert env["ANTHROPIC_API_KEY"] == "a-key"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "o-token"
    assert env["MY_PI_KEY"] == "pi-key"


def test_scrubbed_env_returns_unchanged_when_nothing_configured(home):
    cfg = config_mod.load(str(home))
    assert config_mod.scrubbed_env(cfg, base_env=dict(os.environ)) == dict(os.environ)


# --- [maestro] scrub_env: the config knob itself -----------------------------

def test_scrub_env_non_list_value_fails_config_load_closed(home, capsys):
    (home / "config.toml").write_text('[maestro]\nscrub_env = "GH_TOKEN"\n', encoding="utf-8")
    assert cli_main(["--home", str(home), "status"]) == 2
    assert "scrub_env must be a list of strings" in capsys.readouterr().err


def test_scrub_env_exact_name_and_glob_missing_from_spawn_env(home, monkeypatch):
    monkeypatch.setenv("EXACT_SECRET", "x")
    monkeypatch.setenv("GLOB_SECRET_1", "y")
    (home / "config.toml").write_text(
        '[maestro]\nscrub_env = ["EXACT_SECRET", "GLOB_SECRET_*"]\n', encoding="utf-8")

    sess = ClaudeCliSessions(home=home, capture_session_logs=False)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured = {}

    def capture_popen(*args, **kwargs):
        captured.update(kwargs)
        return fake_proc

    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn("T-1", "p", cwd=home)

    env = captured["env"]
    assert "EXACT_SECRET" not in env
    assert "GLOB_SECRET_1" not in env


# --- AC1: a real dispatch() sweep, all three backends ------------------------

def test_real_dispatch_sweep_hands_claude_spawn_a_scrubbed_env_per_repo(home, monkeypatch):
    monkeypatch.setenv("GH_TOKEN_ALPHA", "tok-alpha")
    monkeypatch.setenv("GH_TOKEN_BETA", "tok-beta")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    # `sessions._spawn_env` reloads config straight off disk (`config.load(home)`)
    # rather than trusting an in-memory `Config` -- write the tracker selection
    # into config.toml itself, not onto a mutated `cfg` object, or the spawn
    # site would never see it.
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nbranch_prefix = "maestro/"\n'
        'min_spawn_interval = 0\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\nslug = "acme/alpha"\ntoken_env = "GH_TOKEN_ALPHA"\n\n'
        '[repos.beta]\npath = "/repo/beta"\nslug = "acme/beta"\ntoken_env = "GH_TOKEN_BETA"\n\n'
        '[providers]\ntracker = "jira"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    _seed(home, "A-1", Phase.READY, repo="alpha")
    _seed(home, "B-1", Phase.READY, repo="beta")

    sess = ClaudeCliSessions(home=home, capture_session_logs=False)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured_by_key = {}

    def capture_popen(cmd, **kwargs):
        # RF-1: argv[2] is the flattened "<command> <key>" prompt.
        key = cmd[2].rsplit(" ", 1)[-1]
        captured_by_key[key] = kwargs
        return fake_proc

    with patch("subprocess.Popen", side_effect=capture_popen):
        report = disp.dispatch(cfg, sess, now=1000)

    assert set(report.spawned) == {"A-1", "B-1"}
    env_a = captured_by_key["A-1"]["env"]
    env_b = captured_by_key["B-1"]["env"]

    assert env_a["GH_TOKEN"] == "tok-alpha"
    assert "GH_TOKEN_ALPHA" not in env_a
    assert "GH_TOKEN_BETA" not in env_a
    assert "JIRA_API_TOKEN" not in env_a

    assert env_b["GH_TOKEN"] == "tok-beta"
    assert "GH_TOKEN_ALPHA" not in env_b
    assert "GH_TOKEN_BETA" not in env_b
    assert "JIRA_API_TOKEN" not in env_b

    # Unrelated ambient variables pass through unchanged for both.
    for env in (env_a, env_b):
        assert env["HOME"] == os.environ["HOME"]
        assert env["ANTHROPIC_API_KEY"] == "anthropic-key"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok"


def test_linear_linked_and_unlinked_tickets_never_leak_linear_api_key_into_spawn_env(
        home, monkeypatch):
    """AC4: LINEAR_API_KEY is dropped from a spawn env whether or not the
    ticket being spawned is the Linear-linked one -- the whole point is that
    NO reconciler ever holds the tracker key, not just an unrelated one's."""
    monkeypatch.setenv("LINEAR_API_KEY", "linear-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nbranch_prefix = "maestro/"\n'
        'min_spawn_interval = 0\n\n[providers]\ntracker = "linear"\n\n[tracker.linear]\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    # `sync_linear_status` (this sweep's own dispatcher hook, T-144) would
    # otherwise make a REAL network call to Linear for the linked ticket below
    # -- stub the transport, same convention as test_linear_status_push.py;
    # never mock anything about the spawn path itself.
    from maestro import providers
    from maestro.providers.linear import LinearTracker
    monkeypatch.setattr(providers, "get_trackers",
                        lambda c: {"linear": LinearTracker({}, transport=MagicMock())})
    _seed(home, "T-1", Phase.READY)  # not Linear-linked

    key = "LINEAR-ENG-1"
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key),
                      "external_source": "linear", "external_id": "ENG-1"}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": "ready"}, actor="r")
    snap_mod.rebuild(home, key)

    sess = ClaudeCliSessions(home=home, capture_session_logs=False)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured_by_key = {}

    def capture_popen(cmd, **kwargs):
        spawned_key = cmd[2].rsplit(" ", 1)[-1]
        captured_by_key[spawned_key] = kwargs
        return fake_proc

    with patch("subprocess.Popen", side_effect=capture_popen):
        report = disp.dispatch(cfg, sess, now=1000)

    assert set(report.spawned) == {"T-1", key}
    assert "LINEAR_API_KEY" not in captured_by_key["T-1"]["env"]
    assert "LINEAR_API_KEY" not in captured_by_key[key]["env"]
    for kwargs in captured_by_key.values():
        assert kwargs["env"]["ANTHROPIC_API_KEY"] == "anthropic-key"


def test_opencode_spawn_env_is_scrubbed_the_same_way(home, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    (home / "config.toml").write_text(
        '[providers]\ntracker = "jira"\n\n[tracker.jira]\nbase_url = "https://x.atlassian.net"\n',
        encoding="utf-8")

    sess = OpencodeCliSessions(home=home, capture_session_logs=False)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured = {}

    def capture_popen(cmd, **kwargs):
        captured.update(kwargs)
        return fake_proc

    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home, runner_model="qwen3-coder:30b")

    env = captured["env"]
    assert "JIRA_API_TOKEN" not in env
    assert env["ANTHROPIC_API_KEY"] == "anthropic-key"


def test_pi_spawn_env_is_scrubbed_the_same_way(home, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    (home / "config.toml").write_text(
        '[providers]\ntracker = "jira"\n\n[tracker.jira]\nbase_url = "https://x.atlassian.net"\n',
        encoding="utf-8")

    sess = PiCliSessions(home=home, capture_session_logs=False)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured = {}

    def capture_popen(cmd, **kwargs):
        captured.update(kwargs)
        return fake_proc

    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home, runner_model="glm-5.2")

    env = captured["env"]
    assert "JIRA_API_TOKEN" not in env
    assert env["ANTHROPIC_API_KEY"] == "anthropic-key"


# --- AC3: dispatcher-run test/check subprocesses also get the scrubbed env --

def test_capture_tests_runs_test_command_with_scrubbed_env(tmp_path, home, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "shh")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo), branch_prefix="maestro/",
                test_command="env > env_dump.txt", scrub_env=["MY_SECRET"])
    wt = _seed_to_worktree(cfg, "E-1", acs=["ok"])

    ops.capture_tests(cfg, "E-1")

    dump = (wt / "env_dump.txt").read_text()
    assert "MY_SECRET=" not in dump
    assert "ANTHROPIC_API_KEY=anthropic-key" in dump


def test_dispatcher_verifying_stage_start_test_run_uses_scrubbed_env(tmp_path, home, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "shh")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    _origin, repo = make_origin_and_repo(tmp_path)
    # No self-redirect here -- `_start_test_run` already wraps the command with
    # its own `> log_path 2>&1`; a second `>` inside `command` itself would
    # just lose the race for stdout. Read the wrapper's own output.log instead
    # -- BEFORE the fold, which unlinks it (`_fold_test_run`).
    cfg = Config(home=home, repo_path=str(repo), branch_prefix="maestro/",
                test_command="env", scrub_env=["MY_SECRET"])
    _seed_to_worktree(cfg, "E-2", acs=["ok"])
    _advance_to_verifying(cfg, "E-2")

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)  # starts the detached test run
    claim = claims.read_claim(home, "E-2")
    _wait_until_dead(claim["pid"])

    dump = disp._test_run_log_path(home, "E-2").read_text()
    assert "MY_SECRET=" not in dump
    assert "ANTHROPIC_API_KEY=anthropic-key" in dump

    disp.dispatch(cfg, sessions, now=1001)  # folds it
    assert snap_mod.load(home, "E-2").phase == Phase.QA.value


def test_run_ac_checks_check_annotation_uses_scrubbed_env(tmp_path, home, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "shh")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo), branch_prefix="maestro/", scrub_env=["MY_SECRET"])
    wt = _seed_to_worktree(cfg, "E-3", acs=["env dump (check: env > env_dump.txt)"])

    ops.run_ac_checks(cfg, "E-3", wt)

    dump = (wt / "env_dump.txt").read_text()
    assert "MY_SECRET=" not in dump
    assert "ANTHROPIC_API_KEY=anthropic-key" in dump


def test_start_restack_popen_env_is_scrubbed(tmp_path, home, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "shh")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    cfg = Config(home=home, repo_path=str(tmp_path / "repo"), branch_prefix="maestro/",
                scrub_env=["MY_SECRET"])
    cwd = tmp_path / "wt"
    cwd.mkdir()
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    dump_path = cwd / "gt_env_dump.txt"
    gt_stub = bin_dir / "gt"
    gt_stub.write_text(f'#!/bin/sh\nenv >> "{dump_path}"\nexit 0\n')
    gt_stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    disp._start_restack(cfg, "E-4", cwd)
    claim = claims.read_claim(home, "E-4")
    _wait_until_dead(claim["pid"])

    dump = dump_path.read_text()
    assert "MY_SECRET=" not in dump
    assert "ANTHROPIC_API_KEY=anthropic-key" in dump
