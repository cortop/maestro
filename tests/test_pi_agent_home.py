"""T-56 (PI-4): a maestro-owned pi agent home (`store.pi_agent_dir`) and a
generated, pinned `models.json` (`store.generate_pi_models_json`), registered
under `$PI_CODING_AGENT_DIR` by the ONE production call site
(`sessions.RoutingSessions.spawn`) so no developer's local `~/.pi` config can
ever shadow what a reconciler resolves.
"""
from __future__ import annotations

import inspect
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from maestro import backup, cli, config as config_mod, dispatcher as disp
from maestro import event_log, health, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions, RoutingSessions
from maestro.statemachine import Phase

_PI_TOML = """\
[maestro]
repo_path = "/repo/default"
branch_prefix = "maestro/"
min_spawn_interval = 0

[runner.pi]
provider = "zai"
base_url = "https://api.z.ai/api/coding/paas/v4"
api = "openai-completions"
version = "0.79.2"
api_key = "$ZAI_API_KEY"

[runner.pi.compat]
supportsDeveloperRole = false
thinkingFormat = "zai"
zaiToolStream = true

[runner.pi.models."glm-5.2"]
context_window = 1048576
max_tokens = 128000
reasoning = true

[runner.pi.models."glm-5.2".cost]
input = 1.4
output = 4.4
cache_read = 0.26
cache_write = 1.4
"""


def _pi_config(home) -> dict:
    return config_mod.load(str(home)).provider_config["runner"]["pi"]


def _seed_ready(home, key):
    store.atomic_write(store.spec_path(home, key),
                        f"# {key}\napproval_tier: 0\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.READY.value}, actor="r")
    snap_mod.rebuild(home, key)


# --- AC1: pi_agent_dir + generator writes models.json via atomic_write -----------

def test_pi_agent_dir_is_dot_pi_agent_under_home(home):
    assert store.pi_agent_dir(home) == home / ".pi-agent"


def test_generate_pi_models_json_writes_the_file(home):
    (home / "config.toml").write_text(_PI_TOML, encoding="utf-8")
    wrote = store.generate_pi_models_json(home, _pi_config(home))
    assert wrote is True
    path = store.pi_agent_dir(home) / "models.json"
    assert path.exists()


# --- AC2: exact parsed-dict shape --------------------------------------------------

def test_generated_models_json_matches_config_exactly(home):
    (home / "config.toml").write_text(_PI_TOML, encoding="utf-8")
    store.generate_pi_models_json(home, _pi_config(home))
    parsed = json.loads((store.pi_agent_dir(home) / "models.json").read_text(encoding="utf-8"))
    assert parsed == {
        "providers": {
            "zai": {
                "baseUrl": "https://api.z.ai/api/coding/paas/v4",
                "api": "openai-completions",
                "apiKey": "$ZAI_API_KEY",
                "compat": {
                    "supportsDeveloperRole": False,
                    "thinkingFormat": "zai",
                    "zaiToolStream": True,
                },
                "models": [
                    {
                        "id": "glm-5.2",
                        "contextWindow": 1048576,
                        "maxTokens": 128000,
                        "reasoning": True,
                        "cost": {
                            "input": 1.4,
                            "output": 4.4,
                            "cacheRead": 0.26,
                            "cacheWrite": 1.4,
                        },
                    }
                ],
            }
        }
    }


def test_generated_models_json_defaults_provider_to_zai(home):
    cfg = {"base_url": "https://api.z.ai/api/coding/paas/v4"}
    store.generate_pi_models_json(home, cfg)
    parsed = json.loads((store.pi_agent_dir(home) / "models.json").read_text(encoding="utf-8"))
    assert set(parsed["providers"]) == {"zai"}


# --- AC3: apiKey is a resolver expression, never resolved -------------------------

def test_api_key_resolver_expression_round_trips_verbatim(home, monkeypatch):
    # An env var this process can see, but that must NEVER be read/interpolated
    # by the generator -- the written file must carry the bare "$..." literal.
    monkeypatch.setenv("ZAI_API_KEY", "sk-should-never-appear-in-models-json")
    cfg = {"api_key": "$ZAI_API_KEY"}
    store.generate_pi_models_json(home, cfg)
    parsed = json.loads((store.pi_agent_dir(home) / "models.json").read_text(encoding="utf-8"))
    assert parsed["providers"]["zai"]["apiKey"] == "$ZAI_API_KEY"
    raw = (store.pi_agent_dir(home) / "models.json").read_text(encoding="utf-8")
    assert "sk-should-never-appear-in-models-json" not in raw

    cfg_cmd = {"api_key": "!op read op://vault/zai/api_key"}
    store.generate_pi_models_json(home, cfg_cmd)
    parsed = json.loads((store.pi_agent_dir(home) / "models.json").read_text(encoding="utf-8"))
    assert parsed["providers"]["zai"]["apiKey"] == "!op read op://vault/zai/api_key"


def test_generate_pi_models_json_has_no_code_path_reading_a_secret_value():
    src = inspect.getsource(store.generate_pi_models_json)
    for banned in ("os.environ", "getenv", "subprocess", "os.system"):
        assert banned not in src, f"generate_pi_models_json must never read/resolve a secret ({banned!r} found)"


# --- AC4: write-if-changed, byte-identical on a second call -----------------------

def test_generate_pi_models_json_is_idempotent_and_reports_no_second_write(home):
    (home / "config.toml").write_text(_PI_TOML, encoding="utf-8")
    pi_cfg = _pi_config(home)
    assert store.generate_pi_models_json(home, pi_cfg) is True
    path = store.pi_agent_dir(home) / "models.json"
    first_bytes = path.read_bytes()
    first_mtime_ns = path.stat().st_mtime_ns

    wrote_again = store.generate_pi_models_json(home, pi_cfg)
    assert wrote_again is False
    assert path.read_bytes() == first_bytes
    assert path.stat().st_mtime_ns == first_mtime_ns  # never re-written, not just same content


# --- AC5: exactly one production call site, exercised by a real dispatcher sweep --

def test_generate_pi_models_json_has_exactly_one_production_call_site():
    maestro_dir = Path(store.__file__).parent
    call_sites = []
    for path in sorted(maestro_dir.glob("*.py")):
        if path.name in ("store.py", "cli.py"):
            # store.py defines it; cli.py's argparse wiring never calls it directly.
            continue
        text = path.read_text(encoding="utf-8")
        if "generate_pi_models_json(" in text:
            call_sites.append(path.name)
    assert call_sites == ["sessions.py"], (
        f"expected exactly one production call site (sessions.py), found: {call_sites}")


def test_real_dispatch_sweep_regenerates_models_json_and_threads_env_var(home):
    (home / "config.toml").write_text(_PI_TOML, encoding="utf-8")
    _seed_ready(home, "T-1")
    delegate = DryRunSessions()
    sessions = RoutingSessions({"claude": delegate}, home=home)
    cfg = config_mod.load(str(home))

    report = disp.dispatch(cfg, sessions, now=1000)
    assert "T-1" in report.spawned

    models_json = store.pi_agent_dir(home) / "models.json"
    assert models_json.exists()
    parsed = json.loads(models_json.read_text(encoding="utf-8"))
    assert "glm-5.2" == parsed["providers"]["zai"]["models"][0]["id"]

    env_overlay = delegate.spawned[0][7]
    assert env_overlay["PI_CODING_AGENT_DIR"] == str(store.pi_agent_dir(home))


def test_routing_sessions_is_a_noop_when_runner_pi_not_configured(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nmin_spawn_interval = 0\n', encoding="utf-8")
    _seed_ready(home, "T-1")
    delegate = DryRunSessions()
    sessions = RoutingSessions({"claude": delegate}, home=home)
    cfg = config_mod.load(str(home))

    disp.dispatch(cfg, sessions, now=1000)

    assert not (store.pi_agent_dir(home) / "models.json").exists()
    env_overlay = delegate.spawned[0][7]
    assert "PI_CODING_AGENT_DIR" not in env_overlay


def test_routing_sessions_without_home_is_a_noop(home):
    # Pre-T-56 call shape (no home kwarg) must keep working unchanged.
    delegate = DryRunSessions()
    sessions = RoutingSessions({"claude": delegate})
    pid = sessions.spawn("T-1", "/maestro-reconcile", home)
    assert pid is None
    assert delegate.spawned[0][7] == {}


# --- AC6: real `pi --list-models` under the generated dir (skip if pi absent) -----

def test_real_pi_list_models_sees_the_pinned_model(home, monkeypatch):
    pi_bin = shutil.which("pi")
    if pi_bin is None:
        import pytest
        pytest.skip("pi is not on PATH")
    (home / "config.toml").write_text(_PI_TOML, encoding="utf-8")
    store.generate_pi_models_json(home, _pi_config(home))
    import os
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(store.pi_agent_dir(home))
    # The resolver expression names $ZAI_API_KEY -- pi requires it to actually
    # resolve (even to a dummy value) before a provider's models count as
    # "available" to --list-models at all. This never reads maestro's own
    # secret store; it's a throwaway value the test itself sets.
    env["ZAI_API_KEY"] = "test-key-for-list-models-only"
    result = subprocess.run([pi_bin, "--list-models", "glm-5.2"],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    assert "glm-5.2" in result.stdout
    assert "zai" in result.stdout
    assert "1.0M" in result.stdout  # the pinned 1,048,576-token context window


# --- AC7: no settings.json written into the agent dir ------------------------------

def test_no_settings_json_written_into_agent_dir(home):
    (home / "config.toml").write_text(_PI_TOML, encoding="utf-8")
    store.generate_pi_models_json(home, _pi_config(home))
    contents = sorted(p.name for p in store.pi_agent_dir(home).iterdir())
    assert contents == ["models.json"]
    assert "settings.json" not in contents


# --- AC8: unknown [runner.pi] key fails closed; DEFAULT_CONFIG_TOML documents it --

def test_unknown_runner_pi_key_makes_doctor_report_an_error(home, capsys):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[runner.pi]\nbogus_key = "nope"\n', encoding="utf-8")
    rc = cli.main(["--home", str(home), "doctor"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "runner.pi" in err and "bogus_key" in err


def test_default_config_toml_documents_runner_pi_table():
    assert "[runner.pi]" in config_mod.DEFAULT_CONFIG_TOML


def test_config_load_accepts_a_well_formed_runner_pi_table(home):
    (home / "config.toml").write_text(_PI_TOML, encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.provider_config["runner"]["pi"]["provider"] == "zai"


# --- AC9: `.pi-agent` excluded from `maestro backup`, restore still succeeds ------

def test_pi_agent_dir_excluded_from_backup_tarball(home):
    (home / "config.toml").write_text(_PI_TOML, encoding="utf-8")
    store.generate_pi_models_json(home, _pi_config(home))
    assert (store.pi_agent_dir(home) / "models.json").exists()

    cfg = config_mod.load(str(home))
    tarball = backup.create_backup(cfg, now=1000)
    import tarfile
    with tarfile.open(tarball) as tar:
        names = tar.getnames()
    assert not any(".pi-agent" in n for n in names)


def test_restore_into_home_without_pi_agent_dir_succeeds(home, tmp_path, capsys):
    (home / "config.toml").write_text(_PI_TOML, encoding="utf-8")
    _seed_ready(home, "T-1")
    store.generate_pi_models_json(home, _pi_config(home))
    rc = cli.main(["--home", str(home), "backup"])
    assert rc == 0
    tarball = json.loads(capsys.readouterr().out)["created"]

    fresh = tmp_path / "restored-home"
    rc = cli.main(["--home", str(fresh), "restore", tarball])
    assert rc == 0
    capsys.readouterr()

    assert not (store.pi_agent_dir(fresh) / "models.json").exists()
    assert (fresh / "events" / "T-1.jsonl").exists()


# --- AC10: pinned pi version + doctor warns on drift -------------------------------

def _fake_run(stdout, returncode=0):
    def run(*args, **kwargs):
        p = MagicMock()
        p.returncode = returncode
        p.stdout = stdout
        p.stderr = ""
        return p
    return run


def test_check_pi_version_ok_when_no_version_pinned(cfg):
    cfg.provider_config = {}
    result = health.check_pi_version(cfg, now=1000)
    assert result["status"] == "ok"
    assert result["installed"] is None


def test_check_pi_version_ok_when_installed_matches_pinned(cfg):
    cfg.provider_config = {"runner": {"pi": {"version": "0.79.2"}}}
    result = health.check_pi_version(cfg, now=1000, run=_fake_run("0.79.2\n"))
    assert result["status"] == "ok"
    assert result["installed"] == "0.79.2"


def test_check_pi_version_warns_on_drift(cfg):
    cfg.provider_config = {"runner": {"pi": {"version": "0.79.2"}}}
    result = health.check_pi_version(cfg, now=1000, run=_fake_run("0.80.0\n"))
    assert result["status"] == "warn"
    assert "0.80.0" in result["detail"] and "0.79.2" in result["detail"]


def test_check_pi_version_registered_in_checks_tuple():
    assert health.check_pi_version in health.CHECKS
