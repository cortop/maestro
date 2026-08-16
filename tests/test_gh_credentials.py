"""GA-17: bind a gh account/token per [repos.<name>] and thread it through both
gh call sites — the spawn env (dispatcher.py -> sessions.spawn) and the
dispatcher's own gh polls (dispatcher.sync_vcs -> providers/cli.py). Fails
closed everywhere: an unresolvable credential never falls back to the ambient
`gh` account, for either call site. The only mocks are the external
boundaries -- the `claude` spawn (`DryRunSessions`) and the `gh` subprocess
(`maestro.providers.cli._run` / the injectable `run=` seam on
`credentials.resolve`/`health.check_gh_credential_reachability`) -- `sessions`,
`repos`, `config`, `health`, and the dispatcher itself are never mocked.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro import config as config_mod
from maestro import credentials, event_log, health, providers, repos as repos_mod
from maestro import snapshot as snap_mod, store
from maestro import dispatcher as disp
from maestro.cli import main as cli_main
from maestro.providers import cli as cli_mod
from maestro.sessions import ClaudeCliSessions, DryRunSessions
from maestro.statemachine import Phase

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed(home, key, phase, *, repo=None, pr=None):
    """Mimic a real `create --repo` mint: TicketCreated.repo + PhaseChanged."""
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\napproval_tier: 0\n\n## Acceptance criteria\n- [ ] ok\n")
    payload = {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}
    if repo:
        payload["repo"] = repo
    event_log.append(home, key, "TicketCreated", payload, actor="d")
    if pr is not None:
        event_log.append(home, key, "PrOpened",
                         {"number": pr, "url": f"https://github.com/x/y/pull/{pr}",
                          "draft": False}, actor="r")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(home, key)


def _write_config(home, *, repos, vcs=None, vcs_sync_interval=0):
    lines = ["[maestro]", 'repo_path = "/repo/default"', 'branch_prefix = "maestro/"',
              "min_spawn_interval = 0"]
    for name, table in repos.items():
        lines.append(f"\n[repos.{name}]")
        for k, v in table.items():
            lines.append(f'{k} = "{v}"')
    if vcs:
        lines.append("\n[providers]")
        lines.append(f'vcs = "{vcs}"')
        lines.append(f"\n[vcs.{vcs}]")
        lines.append(f"sync_interval = {vcs_sync_interval}")
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_mod.load(str(home))


# --- AC: config.py parses gh_account/token_env, RepoBinding carries them ----

def test_config_load_parses_gh_account_and_token_env(home):
    cfg = _write_config(home, repos={
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha", "gh_account": "work-login"},
        "beta": {"path": "/repo/beta", "slug": "acme/beta", "token_env": "GH_TOKEN_BETA"},
    })
    assert cfg.repos["alpha"]["gh_account"] == "work-login"
    assert cfg.repos["alpha"]["token_env"] is None
    assert cfg.repos["beta"]["gh_account"] is None
    assert cfg.repos["beta"]["token_env"] == "GH_TOKEN_BETA"


def test_repo_binding_carries_credential_fields(home):
    cfg = _write_config(home, repos={
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha", "gh_account": "work-login",
                  "token_env": "GH_TOKEN_ALPHA"},
    })
    _seed(home, "T-1", Phase.READY, repo="alpha")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.gh_account == "work-login"
    assert binding.token_env == "GH_TOKEN_ALPHA"


def test_repo_binding_defaults_to_no_credential(home):
    cfg = _write_config(home, repos={"alpha": {"path": "/repo/alpha", "slug": "acme/alpha"}})
    _seed(home, "T-1", Phase.READY, repo="alpha")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.gh_account is None
    assert binding.token_env is None


# --- AC: fail closed, no ambient fallback anywhere --------------------------

def test_config_load_rejects_unrecognized_repo_key(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\ngh_acount = "typo"\n',
        encoding="utf-8")
    with pytest.raises(store.MaestroError) as exc:
        config_mod.load(str(home))
    assert "gh_acount" in str(exc.value)
    assert "alpha" in str(exc.value)


def test_config_load_accepts_credential_fields_without_raising(home):
    # Sanity: the real field names never trip the unknown-key guard.
    _write_config(home, repos={
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha", "gh_account": "x",
                  "token_env": "Y"},
    })


# --- credentials.resolve / resolve_cached / credential_label ----------------

def test_resolve_token_env_reads_the_named_variable(monkeypatch):
    monkeypatch.setenv("GH_TOKEN_X", "tok-x-secret")
    r = credentials.resolve(None, "GH_TOKEN_X")
    assert r.ok
    assert r.env == {"GH_TOKEN": "tok-x-secret"}
    assert r.label == "token_env:GH_TOKEN_X"


def test_resolve_token_env_wins_when_both_set(monkeypatch):
    monkeypatch.setenv("GH_TOKEN_X", "tok-x-secret")
    r = credentials.resolve("some-login", "GH_TOKEN_X")
    assert r.ok
    assert r.env == {"GH_TOKEN": "tok-x-secret"}
    assert r.label == "token_env:GH_TOKEN_X"  # not "gh_account:some-login"


def test_resolve_token_env_unset_fails_closed(monkeypatch):
    monkeypatch.delenv("GH_TOKEN_MISSING", raising=False)
    r = credentials.resolve(None, "GH_TOKEN_MISSING")
    assert not r.ok
    assert r.env is None
    assert "GH_TOKEN_MISSING" in r.error


def test_resolve_token_env_empty_fails_closed(monkeypatch):
    monkeypatch.setenv("GH_TOKEN_EMPTY", "")
    r = credentials.resolve(None, "GH_TOKEN_EMPTY")
    assert not r.ok


def test_resolve_gh_account_shells_gh_auth_token():
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="tok-from-keychain\n", stderr="")
    r = credentials.resolve("cortop", None, run=fake_run)
    assert r.ok
    assert r.env == {"GH_TOKEN": "tok-from-keychain"}
    assert calls == [["gh", "auth", "token", "--user", "cortop"]]


def test_resolve_gh_account_nonzero_exit_fails_closed():
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such account")
    r = credentials.resolve("ghost", None, run=fake_run)
    assert not r.ok
    assert r.env is None
    assert "no such account" in r.error


def test_resolve_neither_set_is_ok_with_no_overlay():
    r = credentials.resolve(None, None)
    assert r.ok
    assert r.env is None
    assert r.label is None


def test_resolve_cached_memoizes_by_gh_account_token_env():
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="tok\n", stderr="")
    cache = {}
    r1 = credentials.resolve_cached("alice", None, cache, run=fake_run)
    r2 = credentials.resolve_cached("alice", None, cache, run=fake_run)
    r3 = credentials.resolve_cached("bob", None, cache, run=fake_run)
    assert r1 is r2  # same cache entry, not just an equal value
    assert r1 is not r3
    assert len(calls) == 2  # alice resolved once, bob resolved once


def test_credential_label_never_resolves_and_never_touches_a_secret(monkeypatch):
    # Set an env var carrying a real-looking secret -- credential_label must never
    # read it (it's a pure name-only helper, no os.environ access, no subprocess).
    monkeypatch.setenv("GH_TOKEN_SECRETY", "super-secret-value")
    assert credentials.credential_label(None, "GH_TOKEN_SECRETY") == "token_env:GH_TOKEN_SECRETY"
    assert credentials.credential_label("cortop", None) == "gh_account:cortop"
    assert credentials.credential_label("cortop", "GH_TOKEN_SECRETY") == "token_env:GH_TOKEN_SECRETY"
    assert credentials.credential_label(None, None) is None


# --- AC: `maestro env --key` reports the name, never the secret -------------

def test_env_key_reports_credential_label_not_secret(home, capsys, monkeypatch):
    monkeypatch.setenv("GH_TOKEN_BETA", "super-secret-token-value")
    _write_config(home, repos={
        "beta": {"path": "/repo/beta", "slug": "acme/beta", "token_env": "GH_TOKEN_BETA"},
    })
    _seed(home, "X-1", Phase.READY, repo="beta")

    capsys.readouterr()
    rc = cli_main(["--home", str(home), "env", "--key", "X-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "super-secret-token-value" not in out
    printed = json.loads(out)
    assert printed["gh_credential"] == "token_env:GH_TOKEN_BETA"


def test_env_key_gh_account_label(home, capsys):
    _write_config(home, repos={
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha", "gh_account": "work-login"},
    })
    _seed(home, "X-2", Phase.READY, repo="alpha")
    capsys.readouterr()
    cli_main(["--home", str(home), "env", "--key", "X-2"])
    printed = json.loads(capsys.readouterr().out)
    assert printed["gh_credential"] == "gh_account:work-login"


def test_env_key_no_credential_configured_reports_none(home, capsys):
    _write_config(home, repos={"alpha": {"path": "/repo/alpha", "slug": "acme/alpha"}})
    _seed(home, "X-3", Phase.READY, repo="alpha")
    capsys.readouterr()
    cli_main(["--home", str(home), "env", "--key", "X-3"])
    printed = json.loads(capsys.readouterr().out)
    assert printed["gh_credential"] is None


def test_env_key_never_shells_gh_auth_token(home, capsys, monkeypatch):
    """`maestro env --key` is a hot path (every reconciler phase preamble calls
    it) -- it must report the credential's NAME without resolving it, so a
    gh_account binding never shells a real `gh auth token` here."""
    _write_config(home, repos={
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha", "gh_account": "work-login"},
    })
    _seed(home, "X-4", Phase.READY, repo="alpha")

    def boom(*a, **k):
        raise AssertionError("maestro env --key must not shell out to resolve a credential")
    monkeypatch.setattr(subprocess, "run", boom)

    capsys.readouterr()
    rc = cli_main(["--home", str(home), "env", "--key", "X-4"])
    assert rc == 0


def test_env_key_never_shells_out_for_a_spec_naming_an_opencode_runner(home, capsys, monkeypatch):
    """RF-2: `dispatcher.resolve_runner` -- called from this same hot path -- stays a
    pure spec+config read, even for a spec naming a runner that isn't registered."""
    store.atomic_write(store.spec_path(home, "X-6"),
                       "# X-6\napproval_tier: 0\nrunner: opencode\n\n## Intent\nx\n")
    event_log.append(home, "X-6", "TicketCreated",
                     {"title": "X-6", "spec_hash": disp.spec_hash_on_disk(home, "X-6")}, actor="d")
    event_log.append(home, "X-6", "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, "X-6")

    def boom(*a, **k):
        raise AssertionError("maestro env --key must not shell out")
    monkeypatch.setattr(subprocess, "run", boom)

    capsys.readouterr()
    rc = cli_main(["--home", str(home), "env", "--key", "X-6"])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["runner"] == "opencode"


# --- AC: sessions.spawn / ClaudeCliSessions.spawn / DryRunSessions.spawn ----

def test_dryrun_records_env_overlay_as_last_positional_element():
    s = DryRunSessions()
    s.spawn("T-1", "prompt", Path("/tmp"), env_overlay={"GH_TOKEN": "tok-x"})
    # Named unpack, not a positional/[-1] index -- see DryRunSessions.spawned's
    # docstring: never assume this tuple stays a fixed length.
    key, prompt, cwd, model, effort, disallowed, allowed, env_overlay, *_ = s.spawned[0]
    assert env_overlay == {"GH_TOKEN": "tok-x"}


def test_dryrun_records_empty_dict_when_env_overlay_not_provided():
    s = DryRunSessions()
    s.spawn("T-1", "prompt", Path("/tmp"))
    key, prompt, cwd, model, effort, disallowed, allowed, env_overlay, *_ = s.spawned[0]
    assert env_overlay == {}


def test_claude_cli_sessions_spawn_merges_env_overlay_beside_maestro_home(home):
    sess = ClaudeCliSessions(home=home, capture_session_logs=False)
    fake_proc = MagicMock()
    import os
    fake_proc.pid = os.getpid()
    captured_kwargs = {}
    def capture_popen(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return fake_proc
    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn("T-1", "p", cwd=home, env_overlay={"GH_TOKEN": "tok-y"})
    env = captured_kwargs["env"]
    assert env["GH_TOKEN"] == "tok-y"
    assert env["MAESTRO_HOME"] == str(home)


def test_claude_cli_sessions_spawn_env_overlay_wins_over_ambient(home, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ambient-value")
    sess = ClaudeCliSessions(home=home, capture_session_logs=False)
    fake_proc = MagicMock()
    import os
    fake_proc.pid = os.getpid()
    captured_kwargs = {}
    def capture_popen(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return fake_proc
    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn("T-1", "p", cwd=home, env_overlay={"GH_TOKEN": "resolved-value"})
    assert captured_kwargs["env"]["GH_TOKEN"] == "resolved-value"


def test_claude_cli_sessions_spawn_env_byte_identical_when_no_overlay(home):
    """AC: back-compat -- no credential configured produces a spawn env
    byte-identical to before this ticket (ambient os.environ + MAESTRO_HOME
    only, nothing extra injected)."""
    import os
    sess = ClaudeCliSessions(home=home, capture_session_logs=False)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured_kwargs = {}
    def capture_popen(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return fake_proc
    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn("T-1", "p", cwd=home)  # no env_overlay
    env = captured_kwargs["env"]
    expected = dict(os.environ)
    expected["MAESTRO_HOME"] = str(home)
    assert env == expected


# --- AC: dispatcher spawn site resolves + threads the overlay --------------

def test_dispatch_spawn_site_threads_correct_overlay_per_repo(home, monkeypatch):
    monkeypatch.setenv("GH_TOKEN_ALPHA", "tok-alpha")
    monkeypatch.setenv("GH_TOKEN_BETA", "tok-beta")
    cfg = _write_config(home, repos={
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha", "token_env": "GH_TOKEN_ALPHA"},
        "beta": {"path": "/repo/beta", "slug": "acme/beta", "token_env": "GH_TOKEN_BETA"},
    })
    _seed(home, "A-1", Phase.READY, repo="alpha")
    _seed(home, "B-1", Phase.READY, repo="beta")

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert set(report.spawned) == {"A-1", "B-1"}

    overlay_by_key = {s[0]: s[-3] for s in sessions.spawned}  # -2 is `runner` (RF-2), -1 `runner_model` (OC-4)
    assert overlay_by_key["A-1"] == {"GH_TOKEN": "tok-alpha"}
    assert overlay_by_key["B-1"] == {"GH_TOKEN": "tok-beta"}


def test_dispatch_spawn_site_no_overlay_when_nothing_configured(home):
    cfg = _write_config(home, repos={"alpha": {"path": "/repo/alpha", "slug": "acme/alpha"}})
    _seed(home, "A-1", Phase.READY, repo="alpha")
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    assert sessions.spawned[0][-3] == {}  # -2 is `runner` (RF-2), -1 `runner_model` (OC-4)


def test_dispatch_spawn_site_memoizes_credential_resolution_per_sweep(home, monkeypatch):
    monkeypatch.setenv("GH_TOKEN_ALPHA", "tok-alpha")
    cfg = _write_config(home, repos={
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha", "token_env": "GH_TOKEN_ALPHA"},
    })
    _seed(home, "A-1", Phase.READY, repo="alpha")
    _seed(home, "A-2", Phase.READY, repo="alpha")

    calls = []
    real_resolve = credentials.resolve
    def counting(gh_account, token_env, *, run=subprocess.run):
        calls.append((gh_account, token_env))
        return real_resolve(gh_account, token_env, run=run)
    monkeypatch.setattr(credentials, "resolve", counting)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert set(report.spawned) == {"A-1", "A-2"}
    # Two keys, same repo/credential -> resolved ONCE for the whole sweep.
    assert calls == [(None, "GH_TOKEN_ALPHA")]


def test_dispatch_credential_unresolvable_blocks_spawn_and_records_visible_failure(home, monkeypatch):
    """AC: fail closed at the spawn site -- no spawn, no ambient fallback, a
    visible failure, and the outcome is readable via `maestro why`."""
    monkeypatch.delenv("GH_TOKEN_MISSING", raising=False)
    cfg = _write_config(home, repos={
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha", "token_env": "GH_TOKEN_MISSING"},
    })
    _seed(home, "A-1", Phase.READY, repo="alpha")

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    assert "A-1" not in report.spawned
    assert sessions.spawned == []  # never touched the spawn boundary at all

    evs = event_log.read(home, "A-1")
    assert any(e["type"] == "Failed" for e in evs)

    rc, out = 0, None
    import io, sys
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli_main(["--home", str(home), "why", "A-1"])
    finally:
        sys.stdout = old
    assert rc == 0
    decisions = json.loads(buf.getvalue())["decisions"]
    assert decisions[-1]["outcome"] == "credential_unresolvable"


# --- AC: the dispatcher's own gh calls (sync_vcs) are covered too ----------

def test_sync_vcs_threads_per_repo_credential_to_real_github_cli_vcs(home, monkeypatch):
    """Real GitHubCliVCS, real sync_vcs, real per-ticket binding resolution --
    the ONLY mock is `providers.cli._run`, the actual gh subprocess boundary."""
    monkeypatch.setenv("GH_TOKEN_ALPHA", "tok-alpha")
    monkeypatch.setenv("GH_TOKEN_BETA", "tok-beta")
    cfg = _write_config(home, repos={
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha", "token_env": "GH_TOKEN_ALPHA"},
        "beta": {"path": "/repo/beta", "slug": "acme/beta", "token_env": "GH_TOKEN_BETA"},
    }, vcs="github_cli")
    _seed(home, "A-1", Phase.AWAITING_CI, repo="alpha", pr=1)
    _seed(home, "B-1", Phase.AWAITING_CI, repo="beta", pr=2)

    calls = []
    def fake_run(cmd, timeout=60, env=None):
        calls.append((list(cmd), env))
        if "reviews" in cmd:
            return 0, '{"reviews": []}', ""
        return 0, json.dumps({"state": "OPEN", "mergeable": "MERGEABLE",
                              "headRefOid": "sha", "statusCheckRollup": []}), ""
    monkeypatch.setattr(cli_mod, "_run", fake_run)

    disp.sync_vcs(cfg, now=1000)

    env_by_repo = {}
    for cmd, env in calls:
        if "--repo" in cmd:
            idx = cmd.index("--repo")
            env_by_repo[cmd[idx + 1]] = env
    assert env_by_repo["acme/alpha"] == {"GH_TOKEN": "tok-alpha"}
    assert env_by_repo["acme/beta"] == {"GH_TOKEN": "tok-beta"}


def test_sync_vcs_unresolvable_credential_never_calls_gh_and_fails_closed(home, monkeypatch):
    """AC: the dispatcher's own poll must not fall back to ambient either --
    proven by asserting `_run` (the gh subprocess boundary) is never invoked,
    and the resulting failure routes through the SAME visible auth-failure
    path a real `gh` credential rejection would (GA-6's classifier)."""
    monkeypatch.delenv("GH_TOKEN_MISSING", raising=False)
    cfg = _write_config(home, repos={
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha", "token_env": "GH_TOKEN_MISSING"},
    }, vcs="github_cli")
    _seed(home, "A-1", Phase.AWAITING_CI, repo="alpha", pr=1)

    calls = []
    def fake_run(cmd, timeout=60, env=None):
        calls.append(cmd)
        return 0, "{}", ""
    monkeypatch.setattr(cli_mod, "_run", fake_run)

    disp.sync_vcs(cfg, now=1000)

    assert calls == []  # never shelled `gh` at all -- no ambient fallback

    evs = event_log.read(home, "A-1")
    ci = [e for e in evs if e["type"] == "CiObserved"]
    assert len(ci) == 1 and ci[0]["payload"]["error"] == "auth"
    failed = [e for e in evs if e["type"] == "Failed"]
    assert len(failed) == 1 and "auth" in failed[0]["payload"]["error"]


# --- AC: `maestro doctor`'s gh_credential_reachability check ----------------

def test_check_gh_credential_reachability_no_repos_no_network_call(cfg):
    def boom(*a, **k):
        raise AssertionError("must not shell out when no [repos.*] table names a slug")
    result = health.check_gh_credential_reachability(cfg, 1000, run=boom)
    assert result["status"] == "ok"
    assert result["unreachable"] == {}


def test_check_gh_credential_reachability_ok_when_reachable(cfg):
    cfg.repos = {"alpha": {"path": "/repo/alpha", "slug": "acme/alpha",
                           "gh_account": None, "token_env": None}}
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
    result = health.check_gh_credential_reachability(cfg, 1000, run=fake_run)
    assert result["status"] == "ok"
    assert result["unreachable"] == {}


def test_check_gh_credential_reachability_warns_when_unreachable(cfg):
    cfg.repos = {"alpha": {"path": "/repo/alpha", "slug": "acme/alpha",
                           "gh_account": None, "token_env": None}}
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 404: Not Found")
    result = health.check_gh_credential_reachability(cfg, 1000, run=fake_run)
    assert result["status"] == "warn"
    assert "alpha" in result["unreachable"]


def test_check_gh_credential_reachability_warns_when_credential_unresolvable(cfg, monkeypatch):
    monkeypatch.delenv("GH_TOKEN_MISSING", raising=False)
    cfg.repos = {"alpha": {"path": "/repo/alpha", "slug": "acme/alpha",
                           "gh_account": None, "token_env": "GH_TOKEN_MISSING"}}
    def boom(*a, **k):
        raise AssertionError("must not call `gh repo view` with an unresolvable credential")
    result = health.check_gh_credential_reachability(cfg, 1000, run=boom)
    assert result["status"] == "warn"
    assert "credential unresolvable" in result["unreachable"]["alpha"]


def test_check_gh_credential_reachability_never_blocks_a_spawn(home, monkeypatch):
    """A WARN-only check: `maestro doctor`'s default (non-strict) exit code
    stays 0 even when a repo is unreachable."""
    _write_config(home, repos={"alpha": {"path": "/repo/alpha", "slug": "acme/alpha"}})

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = cli_main(["--home", str(home), "doctor"])
    assert rc == 0


def test_doctor_cli_reports_gh_credential_reachability_check(home, monkeypatch, capsys):
    _write_config(home, repos={"alpha": {"path": "/repo/alpha", "slug": "acme/alpha"}})

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    capsys.readouterr()
    rc = cli_main(["--home", str(home), "doctor"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    names = {c["name"] for c in out["checks"]}
    assert "gh_credential_reachability" in names


# --- AC: no secret leaks anywhere in the home -------------------------------

def test_no_secret_leak_across_a_full_sweep_with_credential_configured(home, monkeypatch):
    SECRET = "sekrit-token-do-not-leak-abc123"
    monkeypatch.setenv("GH_TOKEN_ALPHA", SECRET)
    cfg = _write_config(home, repos={
        "alpha": {"path": "/repo/alpha", "slug": "acme/alpha", "token_env": "GH_TOKEN_ALPHA"},
    })
    _seed(home, "A-1", Phase.READY, repo="alpha")

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    for p in home.rglob("*"):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        assert SECRET not in text, f"secret leaked into {p}"


# --- AC: back-compat -- no credential fields anywhere is byte-identical -----

def test_dispatch_no_credentials_configured_is_byte_identical_env(home):
    cfg = _write_config(home, repos={"alpha": {"path": "/repo/alpha", "slug": "acme/alpha"}})
    _seed(home, "A-1", Phase.READY, repo="alpha")
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    key, prompt, cwd, model, effort, disallowed, allowed, overlay, *_ = sessions.spawned[0]
    assert overlay == {}


# --- AC: DOGFOOD.md documents the credential-binding step -------------------

def test_dogfood_documents_credential_binding_and_corrected_constraint():
    text = (REPO_ROOT / "DOGFOOD.md").read_text()
    assert "gh_account" in text
    assert "token_env" in text
    assert "gh_credential_reachability" in text
    assert "corrected constraint" in text  # supersedes the old max_concurrency=1 claim
    assert "git push" in text  # the overlay's documented limitation
