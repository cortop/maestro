"""The suite must not depend on the developer's git signing setup.

`tests/conftest.py`'s shared `git` helper runs a REAL `git commit` with
`check=True`, ~200 times per full run. While it inherited the host's
`commit.gpgsign`, every one of those commits needed an unlocked signing agent
-- so a locked 1Password/YubiKey turned the suite red in a place that names
nothing about signing, as a multi-second stall ending in exit 128.

This drives the real helper with signing forced ON and pointed at a signer
that cannot exist, which is the closest reproducible stand-in for a refusing
agent.
"""
import subprocess

import pytest

from conftest import git, make_origin_and_repo


HOSTILE = {
    "GIT_CONFIG_COUNT": "3",
    "GIT_CONFIG_KEY_0": "commit.gpgsign", "GIT_CONFIG_VALUE_0": "true",
    "GIT_CONFIG_KEY_1": "gpg.format", "GIT_CONFIG_VALUE_1": "ssh",
    "GIT_CONFIG_KEY_2": "gpg.ssh.program", "GIT_CONFIG_VALUE_2": "/nonexistent/signer",
}


def test_a_refusing_signing_agent_cannot_break_the_helper(tmp_path, monkeypatch):
    for k, v in HOSTILE.items():
        monkeypatch.setenv(k, v)
    # Sanity: this environment really does break an un-guarded commit, so the
    # assertion below is testing something.
    repo = tmp_path / "bare-check"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    unguarded = subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=repo,
                               capture_output=True, text=True)
    assert unguarded.returncode != 0, "the hostile env no longer breaks a plain commit"

    # The helper must be immune.
    origin, clone = make_origin_and_repo(tmp_path, name="proj")
    (clone / "second.txt").write_text("y\n")
    git("add", "-A", cwd=clone)
    git("commit", "-q", "-m", "second", cwd=clone)
    log = subprocess.run(["git", "log", "--oneline"], cwd=clone,
                         capture_output=True, text=True, check=True)
    assert "second" in log.stdout


def test_helper_forces_signing_off_for_tags_too(tmp_path, monkeypatch):
    for k, v in HOSTILE.items():
        monkeypatch.setenv(k, v)
    _origin, clone = make_origin_and_repo(tmp_path, name="tagproj")
    git("tag", "-a", "v1", "-m", "v1", cwd=clone)
    out = subprocess.run(["git", "tag", "-l"], cwd=clone,
                         capture_output=True, text=True, check=True)
    assert "v1" in out.stdout
