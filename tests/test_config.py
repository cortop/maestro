"""`config.KNOBS`: the single declaration of every [maestro] key. These pin the
properties the table exists for -- a typo'd key fails loudly, every declared knob
is a real field, per-repo overrides resolve generically -- through the real CLI
and the real `config.load` over a temp home."""
from __future__ import annotations

import dataclasses
import inspect
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
    "allowed_paths": ["src/**"],
    "base_drift_policy": "on_conflict",
    "ci_auto_rerun": True,
    "ci_failure_excerpt": True,
    "ci_rerun_grace": 7,
    "file_hints": True,
    "language": "go",
    "post_qa_skill": "/polish",
    "post_qa_skill_runner": "pi",
    "post_qa_skill_runner_model": "m",
    "pr_split_threshold": 0,
    "prime_timeout": 6,
    "test_command": "make check",
    "worktree_timeout": 5,
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


# --- T-183: knobs live in themed sections, alphabetical within each ------------

_SECTION_RE = re.compile(r"^\s*# --- (.+) ---$")


def _grouped(lines, name_re):
    """[(section header, [names...])] for the lines of one list, in source order."""
    out = []
    for line in lines:
        header = _SECTION_RE.match(line)
        if header:
            out.append((header.group(1), []))
            continue
        name = name_re.match(line)
        if name and out:
            out[-1][1].append(name.group(1))
    return out


def _body(source, start, end_re):
    text = source[source.index(start):]
    return text[:re.search(end_re, text).start()].splitlines()


def _knob_only(groups, names):
    """Drop non-knob names and any section left empty (Config's trailing
    state/table fields are not [maestro] knobs)."""
    kept = [(title, [n for n in ns if n in names]) for title, ns in groups]
    return [(title, ns) for title, ns in kept if ns]


def _knob_sections():
    source = inspect.getsource(config_mod)
    names = config_mod.MAESTRO_KEYS
    config_lines = _body(source, "class Config:", r"\n\n\n")
    knob_lines = _body(source, "KNOBS: tuple[Knob, ...] = (", r"\n\)\n")
    toml = config_mod.DEFAULT_CONFIG_TOML
    template_lines = toml[toml.index("[maestro]"):toml.index("\n[providers]")].splitlines()
    return {
        "Config": _knob_only(_grouped(config_lines, re.compile(r"^    ([a-z_]+): ")), names),
        "KNOBS": _knob_only(_grouped(knob_lines, re.compile(r'^\s+Knob\("([a-z_]+)"')), names),
        "template": _knob_only(_grouped(template_lines, re.compile(r"^#?\s*([a-z_]+)\s*=")), names),
    }


def test_knob_sections_sorted_and_aligned():
    """The Config fields, KNOBS rows and the [maestro] template block share the
    same `# --- <section> ---` headers in the same order, the same knobs per
    section, alphabetical within each -- so a new knob has ONE deterministic slot
    and concurrent knob tickets stop conflicting at a shared tail."""
    lists = _knob_sections()
    assert lists["KNOBS"], "no sections found in config.KNOBS"
    assert lists["Config"] == lists["KNOBS"], "Config fields drifted from KNOBS"
    assert lists["template"] == lists["KNOBS"], "DEFAULT_CONFIG_TOML drifted from KNOBS"
    for title, names in lists["KNOBS"]:
        assert names == sorted(names), f"section {title!r} is not alphabetical"
    every = [n for _, names in lists["KNOBS"] for n in names]
    assert sorted(every) == sorted(config_mod.MAESTRO_KEYS), "a knob sits outside any section"


def test_knob_section_check_catches_an_out_of_order_knob(monkeypatch):
    """Moving one knob out of its slot must make the alignment check go red."""
    toml = config_mod.DEFAULT_CONFIG_TOML
    moved = toml.replace("backoff_base = 30", "backoff_base_moved = 30", 1)
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_TOML", moved)
    lists = _knob_sections()
    assert lists["template"] != lists["KNOBS"]


def test_repo_binding_fields_follow_knob_order():
    """After its 5 required fields, `RepoBinding` declares the repo-only fields
    first, then the per-repo override fields under the same section headers, in
    the same order as `config.KNOBS`."""
    lines = _body(inspect.getsource(repos_mod), "class RepoBinding:", r"\n\n\n")
    fields = dataclasses.fields(repos_mod.RepoBinding)
    required = [f.name for f in fields if f.default is dataclasses.MISSING
                and f.default_factory is dataclasses.MISSING]
    assert required == ["name", "path", "slug", "base_branch", "branch_prefix"]
    repo_only, *override_groups = _grouped(lines, re.compile(r"^    ([a-z_]+): "))
    assert repo_only[0].startswith("repo-only")
    assert repo_only[1] == sorted(repo_only[1])
    overrides = set(config_mod.REPO_OVERRIDE_KEYS)
    expected = [(t, [n for n in ns if n in overrides]) for t, ns in _knob_sections()["KNOBS"]]
    expected = [(t, ns) for t, ns in expected if ns]
    assert override_groups == expected
    assert [f.name for f in fields][5:] == repo_only[1] + [n for _, ns in expected for n in ns]
