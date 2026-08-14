"""OC-4: `[runner.opencode]` is explicitly validated (fail-closed, same
posture as `_REPO_TABLE_KEYS`) -- unlike every other `[runner.<name>]`/
`[tracker.*]`/etc table, which stays free-form, opaque `provider_config`
(spec Notes: "that passthrough is not fail-closed... a typo is silent").
An unrecognized key makes real `maestro doctor` report an error (config.load
raises, cli.main prints it and exits non-zero) rather than silently landing
in `cfg.provider_config` unused. Also covers `dispatcher._runner_concurrency_cap`,
the one thing `[runner.opencode]`'s recognized key actually configures.
"""
from __future__ import annotations

import io
import sys

import pytest

from maestro import cli, config as config_mod, dispatcher as disp, store
from maestro.config import Config, DEFAULT_CONFIG_TOML


def _run_doctor(home):
    buf = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf, err
    try:
        code = cli.main(["--home", str(home), "doctor"])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, buf.getvalue(), err.getvalue()


# --- fail-closed at config.load -----------------------------------------

def test_config_load_rejects_unrecognized_runner_opencode_key(home):
    (home / "config.toml").write_text(
        '[runner.opencode]\nconcurrenc = 1\n', encoding="utf-8")  # typo
    with pytest.raises(store.MaestroError) as exc:
        config_mod.load(str(home))
    assert "runner.opencode" in str(exc.value)
    assert "concurrenc" in str(exc.value)


def test_config_load_accepts_the_recognized_concurrency_key(home):
    (home / "config.toml").write_text(
        '[runner.opencode]\nconcurrency = 2\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.provider_config["runner"]["opencode"]["concurrency"] == 2


def test_config_load_tolerates_no_runner_table_at_all(home):
    cfg = config_mod.load(str(home))  # no config.toml at all
    assert cfg.provider_config == {}


def test_config_load_tolerates_other_runner_tables_unvalidated(home):
    """Only opencode's own table is explicitly validated -- a future
    `[runner.somethingelse]` table stays free-form, per `provider_config`'s
    own docstring."""
    (home / "config.toml").write_text(
        '[runner.somethingelse]\nwhatever = "goes"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.provider_config["runner"]["somethingelse"] == {"whatever": "goes"}


# --- "maestro doctor report an error rather than silently ignoring it" -----

def test_real_doctor_reports_an_error_on_unrecognized_runner_opencode_key(home):
    (home / "config.toml").write_text(
        '[runner.opencode]\nbogus_key = "x"\n', encoding="utf-8")
    for d in ("events", "inbox", "tickets", "worktrees", "derived/snapshots", "derived/cursors"):
        (home / d).mkdir(parents=True, exist_ok=True)

    code, out, err = _run_doctor(home)

    assert code != 0
    assert "error" in err.lower()
    assert "runner.opencode" in err
    assert "bogus_key" in err
    assert out == ""  # never got far enough to print a report


def test_real_doctor_succeeds_with_a_recognized_runner_opencode_key(home):
    (home / "config.toml").write_text(
        '[runner.opencode]\nconcurrency = 1\n', encoding="utf-8")
    for d in ("events", "inbox", "tickets", "worktrees", "derived/snapshots", "derived/cursors"):
        (home / d).mkdir(parents=True, exist_ok=True)

    code, out, err = _run_doctor(home)

    assert code == 0
    assert err == ""


def test_default_config_toml_documents_runner_opencode():
    assert "[runner.opencode]" in DEFAULT_CONFIG_TOML
    assert "concurrency" in DEFAULT_CONFIG_TOML


# --- dispatcher._runner_concurrency_cap --------------------------------------

def test_runner_concurrency_cap_defaults_to_one_for_opencode(home):
    cfg = Config(home=home)
    assert disp._runner_concurrency_cap(cfg, "opencode") == 1


def test_runner_concurrency_cap_reads_the_configured_value(home):
    cfg = Config(home=home, provider_config={"runner": {"opencode": {"concurrency": 4}}})
    assert disp._runner_concurrency_cap(cfg, "opencode") == 4


def test_runner_concurrency_cap_none_for_claude(home):
    cfg = Config(home=home)
    assert disp._runner_concurrency_cap(cfg, "claude") is None


def test_runner_concurrency_cap_none_for_an_unconfigured_unknown_runner(home):
    cfg = Config(home=home)
    assert disp._runner_concurrency_cap(cfg, "some-future-runner") is None


# --- T-54: [runner.opencode]'s `phases` key -----------------------------

def test_config_load_accepts_the_recognized_phases_key(home):
    (home / "config.toml").write_text(
        '[runner.opencode]\nphases = ["qa", "triaging"]\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.provider_config["runner"]["opencode"]["phases"] == ["qa", "triaging"]


def test_config_load_accepts_both_concurrency_and_phases_together(home):
    (home / "config.toml").write_text(
        '[runner.opencode]\nconcurrency = 2\nphases = ["qa"]\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.provider_config["runner"]["opencode"]["concurrency"] == 2
    assert cfg.provider_config["runner"]["opencode"]["phases"] == ["qa"]


def test_default_config_toml_documents_runner_phases():
    assert "phases" in DEFAULT_CONFIG_TOML
