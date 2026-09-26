"""`config.KNOBS`: the single declaration of every [maestro] key. These pin the
properties the table exists for -- a typo'd key fails loudly, every declared knob
is a real field, per-repo overrides resolve generically -- through the real CLI
and the real `config.load` over a temp home."""
from __future__ import annotations

import dataclasses
import json
import re

import pytest

from maestro import config as config_mod, repos as repos_mod, store
from maestro.cli import main as cli_main


def _write(home, text):
    (home / "config.toml").write_text(text, encoding="utf-8")


def test_unknown_maestro_key_fails_every_verb_closed(home, capsys):
    """A typo'd [maestro] key used to be dropped without a word, silently
    leaving the default in force (`max_failure` -> max_failures stays 4)."""
    _write(home, "[maestro]\nmax_failure = 2\n")
    assert cli_main(["--home", str(home), "status"]) == 2
    err = capsys.readouterr().err
    assert "[maestro] has unrecognized key(s): max_failure" in err


def test_non_integer_knob_is_a_config_error_not_a_traceback(home, capsys):
    _write(home, '[maestro]\nmax_concurrency = "lots"\n')
    assert cli_main(["--home", str(home), "status"]) == 2
    assert "[maestro] max_concurrency must be an integer, got 'lots'" in capsys.readouterr().err


def test_known_knobs_load_through_the_real_cli(home, capsys):
    _write(home, "[maestro]\nmax_concurrency = 7\nbase_drift_policy = \"daily\"\n")
    assert cli_main(["--home", str(home), "env"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["max_concurrency"] == 7
    cfg = config_mod.load(str(home))
    assert cfg.base_drift_policy == "daily"


def test_absent_knob_keeps_the_dataclass_default(home):
    _write(home, "[maestro]\n")
    cfg = config_mod.load(str(home))
    defaults = config_mod.Config(home=home)
    for knob in config_mod.KNOBS:
        assert getattr(cfg, knob.name) == getattr(defaults, knob.name), knob.name


def test_every_knob_is_a_config_field():
    fields = {f.name for f in dataclasses.fields(config_mod.Config)}
    assert not [k.name for k in config_mod.KNOBS if k.name not in fields]
    assert "bash_max_timeout" in fields


def test_every_per_repo_knob_is_a_repo_binding_field():
    fields = {f.name for f in dataclasses.fields(repos_mod.RepoBinding)}
    assert not [k for k in config_mod.REPO_OVERRIDE_KEYS if k not in fields]


def test_default_config_template_names_only_recognized_keys():
    """Every [maestro] key `maestro init` writes -- set or commented out -- must
    load, or a human uncommenting one gets an unrecognized-key error."""
    section, named = None, set()
    for line in config_mod.DEFAULT_CONFIG_TOML.splitlines():
        header = re.match(r"^\[([^\]]+)\]", line)
        if header:
            section = header.group(1)
            continue
        key = re.match(r"^#?\s*([a-z_]+)\s*=", line)
        if key and section == "maestro":
            named.add(key.group(1))
    assert named, "no [maestro] keys found in DEFAULT_CONFIG_TOML"
    assert not sorted(named - config_mod.MAESTRO_KEYS)


@pytest.mark.parametrize("key", config_mod.REPO_OVERRIDE_KEYS)
def test_repo_override_unset_inherits_board_wide_value(home, key):
    _write(home, f'[repos.a]\npath = "{home}"\n')
    cfg = config_mod.load(str(home))
    binding = repos_mod._binding_from_table(cfg, "a", cfg.repos["a"])
    assert getattr(binding, key) == getattr(cfg, key)


_OVERRIDE_SAMPLES = {
    "worktree_timeout": 5, "prime_timeout": 6, "ci_auto_rerun": True, "ci_rerun_grace": 7,
    "ci_failure_excerpt": True, "pr_split_threshold": 0, "test_command": "make check",
    "language": "go", "post_qa_skill": "/polish", "post_qa_skill_runner": "pi",
    "post_qa_skill_runner_model": "m", "file_hints": True, "base_drift_policy": "on_conflict",
}


def test_override_samples_cover_every_per_repo_knob():
    assert set(_OVERRIDE_SAMPLES) == set(config_mod.REPO_OVERRIDE_KEYS)


@pytest.mark.parametrize("key", config_mod.REPO_OVERRIDE_KEYS)
def test_repo_override_set_wins_over_board_wide_value(home, key):
    value = _OVERRIDE_SAMPLES[key]
    toml_value = json.dumps(value) if not isinstance(value, bool) else str(value).lower()
    _write(home, f'[repos.a]\npath = "{home}"\n{key} = {toml_value}\n')
    cfg = config_mod.load(str(home))
    binding = repos_mod._binding_from_table(cfg, "a", cfg.repos["a"])
    assert getattr(binding, key) == value


def test_repo_override_is_validated_like_the_board_wide_knob(home):
    _write(home, f'[repos.a]\npath = "{home}"\npr_split_threshold = -1\n')
    with pytest.raises(store.MaestroError, match=r"\[repos\.a\] pr_split_threshold must be >= 0"):
        config_mod.load(str(home))
    _write(home, "[maestro]\npr_split_threshold = -1\n")
    with pytest.raises(store.MaestroError, match=r"\[maestro\] pr_split_threshold must be >= 0"):
        config_mod.load(str(home))
