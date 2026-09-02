"""T-105: `[runner.pi.headers]` -- custom HTTP headers a gateway requires
beyond the bearer `api_key` (e.g. an internal auth proxy rejecting requests
missing a `source`/`org-id` header), threaded verbatim into the generated
`models.json` provider block by `store.generate_pi_models_json`. Follows
`tests/test_pi_agent_home.py`'s conventions: exact-shape assertion,
verbatim-no-secret-read guard, idempotency/no-op regression, and a
doctor-based fail-closed test for a malformed table.
"""
from __future__ import annotations

import inspect
import json

from maestro import cli, config as config_mod, store

_PI_TOML_NO_HEADERS = """\
[maestro]
repo_path = "/repo/default"
branch_prefix = "maestro/"
min_spawn_interval = 0

[runner.pi]
provider = "zai"
base_url = "https://api.z.ai/api/coding/paas/v4"
api = "openai-completions"
api_key = "$ZAI_API_KEY"
"""

_PI_TOML_WITH_HEADERS = """\
[maestro]
repo_path = "/repo/default"
branch_prefix = "maestro/"
min_spawn_interval = 0

[runner.pi]
provider = "zai"
base_url = "https://api.z.ai/api/coding/paas/v4"
api = "openai-completions"
api_key = "$ZAI_API_KEY"

[runner.pi.headers]
source = "maestro"
org-id = "acme-corp"
x-dd-tag-team = "platform"
x-llmo-force-redaction = "true"
"""


def _pi_config(home) -> dict:
    return config_mod.load(str(home)).provider_config["runner"]["pi"]


# --- AC1: config.load() accepts a well-formed [runner.pi.headers] table, ----------
# rejects a malformed one, other unknown [runner.pi] keys still fail closed -------

def test_config_load_accepts_a_well_formed_headers_table(home):
    (home / "config.toml").write_text(_PI_TOML_WITH_HEADERS, encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.provider_config["runner"]["pi"]["headers"] == {
        "source": "maestro",
        "org-id": "acme-corp",
        "x-dd-tag-team": "platform",
        "x-llmo-force-redaction": "true",
    }


def test_config_load_rejects_non_table_headers(home, capsys):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[runner.pi]\nheaders = "not-a-table"\n', encoding="utf-8")
    rc = cli.main(["--home", str(home), "doctor"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "runner.pi.headers" in err


def test_config_load_rejects_headers_with_non_string_value(home, capsys):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[runner.pi.headers]\nsource = 1\n', encoding="utf-8")
    rc = cli.main(["--home", str(home), "doctor"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "runner.pi.headers" in err


def test_unrecognized_runner_pi_key_still_fails_closed_alongside_headers(home, capsys):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[runner.pi]\nbogus_key = "nope"\n\n'
        '[runner.pi.headers]\nsource = "maestro"\n', encoding="utf-8")
    rc = cli.main(["--home", str(home), "doctor"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "runner.pi" in err and "bogus_key" in err


def test_default_config_toml_documents_runner_pi_headers_table():
    assert "[runner.pi.headers]" in config_mod.DEFAULT_CONFIG_TOML


# --- AC2: generate_pi_models_json emits headers verbatim under the key -------------
# pi's model registry actually reads ("headers" -- confirmed against the -----------
# installed pi 0.79.2's core/model-registry.js: ProviderConfigSchema.headers) ------

def test_generated_models_json_emits_headers_under_the_confirmed_key(home):
    (home / "config.toml").write_text(_PI_TOML_WITH_HEADERS, encoding="utf-8")
    store.generate_pi_models_json(home, _pi_config(home))
    parsed = json.loads((store.pi_agent_dir(home) / "models.json").read_text(encoding="utf-8"))
    assert parsed["providers"]["zai"]["headers"] == {
        "source": "maestro",
        "org-id": "acme-corp",
        "x-dd-tag-team": "platform",
        "x-llmo-force-redaction": "true",
    }


def test_generate_pi_models_json_has_no_code_path_reading_a_secret_value_for_headers():
    # Same guard as AC3 in test_pi_agent_home.py -- headers are written through
    # verbatim, same posture as api_key, never read/resolved by this function.
    src = inspect.getsource(store.generate_pi_models_json)
    for banned in ("os.environ", "getenv", "subprocess", "os.system"):
        assert banned not in src, (
            f"generate_pi_models_json must never read/resolve a secret ({banned!r} found)")


# --- AC3: with no headers configured, models.json is byte-identical to before -----
# this change (regression: the write-if-changed no-op still holds) -----------------

def test_no_headers_configured_omits_the_key_entirely(home):
    (home / "config.toml").write_text(_PI_TOML_NO_HEADERS, encoding="utf-8")
    store.generate_pi_models_json(home, _pi_config(home))
    parsed = json.loads((store.pi_agent_dir(home) / "models.json").read_text(encoding="utf-8"))
    assert "headers" not in parsed["providers"]["zai"]


def test_no_headers_configured_matches_the_pre_headers_exact_shape(home):
    (home / "config.toml").write_text(_PI_TOML_NO_HEADERS, encoding="utf-8")
    store.generate_pi_models_json(home, _pi_config(home))
    parsed = json.loads((store.pi_agent_dir(home) / "models.json").read_text(encoding="utf-8"))
    assert parsed == {
        "providers": {
            "zai": {
                "baseUrl": "https://api.z.ai/api/coding/paas/v4",
                "api": "openai-completions",
                "apiKey": "$ZAI_API_KEY",
            }
        }
    }


def test_generate_pi_models_json_still_idempotent_and_no_op_with_headers_configured(home):
    (home / "config.toml").write_text(_PI_TOML_WITH_HEADERS, encoding="utf-8")
    pi_cfg = _pi_config(home)
    assert store.generate_pi_models_json(home, pi_cfg) is True
    path = store.pi_agent_dir(home) / "models.json"
    first_bytes = path.read_bytes()
    first_mtime_ns = path.stat().st_mtime_ns

    wrote_again = store.generate_pi_models_json(home, pi_cfg)
    assert wrote_again is False
    assert path.read_bytes() == first_bytes
    assert path.stat().st_mtime_ns == first_mtime_ns  # never re-written, not just same content


# --- AC4: a config carrying the real gateway's headers round-trips into -----------
# models.json verbatim, values untouched --------------------------------------------

def test_real_gateway_headers_round_trip_verbatim(home):
    gateway_toml = """\
[maestro]
repo_path = "/repo/default"
min_spawn_interval = 0

[runner.pi]
provider = "zai"
api_key = "$ZAI_API_KEY"

[runner.pi.headers]
source = "maestro"
org-id = "internal-gateway-org"
x-dd-tag-team = "eng-platform"
x-dd-tag-env = "prod"
x-llmo-force-redaction = "true"
"""
    (home / "config.toml").write_text(gateway_toml, encoding="utf-8")
    store.generate_pi_models_json(home, _pi_config(home))
    parsed = json.loads((store.pi_agent_dir(home) / "models.json").read_text(encoding="utf-8"))
    assert parsed["providers"]["zai"]["headers"] == {
        "source": "maestro",
        "org-id": "internal-gateway-org",
        "x-dd-tag-team": "eng-platform",
        "x-dd-tag-env": "prod",
        "x-llmo-force-redaction": "true",
    }
