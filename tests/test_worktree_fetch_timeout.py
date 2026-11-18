"""`worktree_fetch_timeout` bounds `worktree ensure`'s `git fetch origin <base>`,
board-wide with a `[repos.<name>]` override."""
from __future__ import annotations

import subprocess

from maestro import config as config_mod
from maestro import ops, store
from maestro.cli import main as cli_main

from conftest import make_origin_and_repo


def _spy_fetch_timeouts(monkeypatch, repo, *, hang=False):
    seen = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        if cmd[:4] == ["git", "-C", str(repo), "fetch"]:
            seen.append(kwargs.get("timeout"))
            if hang:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    return seen


def _seed_spec(home, key):
    store.atomic_write(store.spec_path(home, key), f"# {key}\n\n## Intent\nx\n")


def test_fetch_uses_board_wide_default(home, tmp_path, monkeypatch):
    _, repo = make_origin_and_repo(tmp_path, name="target")
    (home / "config.toml").write_text(f'[maestro]\nrepo_path = "{repo}"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    _seed_spec(home, "FT-1")
    seen = _spy_fetch_timeouts(monkeypatch, repo)

    ops.worktree_ensure(cfg, "FT-1")

    assert seen == [240]


def test_fetch_uses_per_repo_override(home, tmp_path, monkeypatch):
    _, repo = make_origin_and_repo(tmp_path, name="target")
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/unused"\nworktree_fetch_timeout = 100\n\n'
        f'[repos.alpha]\npath = "{repo}"\ndefault = true\nworktree_fetch_timeout = 300\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    _seed_spec(home, "FT-2")
    seen = _spy_fetch_timeouts(monkeypatch, repo)

    ops.worktree_ensure(cfg, "FT-2")

    assert seen == [300]


def test_fetch_timeout_error_names_the_knob(home, tmp_path, monkeypatch, capsys):
    _, repo = make_origin_and_repo(tmp_path, name="target")
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{repo}"\nworktree_fetch_timeout = 9\n', encoding="utf-8")
    _seed_spec(home, "FT-3")
    _spy_fetch_timeouts(monkeypatch, repo, hang=True)
    capsys.readouterr()

    rc = cli_main(["--home", str(home), "worktree", "ensure", "FT-3"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "within 9s" in err
    assert "worktree_fetch_timeout" in err
