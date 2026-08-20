"""GA-15: `maestro install-commands` — automated distribution of the seven
per-phase `.claude/commands/maestro-reconcile-*.md` files, replacing the
"vendor them by hand" step DOGFOOD.md used to document. Two targets
(`--repo <name>` copy, `--user` symlink), both idempotent; the payload ships
in the wheel and resolves from the installed package, never a repo-root-
relative path (TRAP A); the doctor check treats a user-scope install as
satisfied without depending on the developer's real `~/.claude` (TRAP B).
"""
from __future__ import annotations

import importlib.resources
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from maestro import dispatcher as disp, event_log, skills_install, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.config import Config
from maestro.statemachine import Phase

from conftest import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"


def _write_config(home: Path, text: str) -> None:
    (home / "config.toml").write_text(text)


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / "README.md").write_text("hi\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)


# ---------------------------------------------------------------------------
# AC1/AC3: --repo copies all seven, idempotently, no duplication
# ---------------------------------------------------------------------------

def test_install_repo_copies_six_files_byte_identical(home, tmp_path):
    repo = tmp_path / "acme"
    _write_config(home, f'[repos.acme]\npath = "{repo}"\n')

    rc = cli_main(["--home", str(home), "install-commands", "--repo", "acme"])
    assert rc == 0

    commands_dir = repo / ".claude" / "commands"
    installed = sorted(p.name for p in commands_dir.iterdir())
    assert installed == sorted(skills_install.PAYLOAD_NAMES)
    for name in skills_install.PAYLOAD_NAMES:
        assert (commands_dir / name).read_text() == (COMMANDS_DIR / name).read_text()


# ---------------------------------------------------------------------------
# OC-1 AC1: --repo ALSO installs an opencode copy from the same source, one
# documented frontmatter transform apart -- byte-identical body, no third
# hand-maintained source file.
# ---------------------------------------------------------------------------

def test_install_repo_also_installs_opencode_copy_with_documented_frontmatter_transform(home, tmp_path):
    repo = tmp_path / "acme"
    _write_config(home, f'[repos.acme]\npath = "{repo}"\n'
                        '[maestro]\nrunner_enabled = ["claude", "opencode"]\n')

    rc = cli_main(["--home", str(home), "install-commands", "--repo", "acme"])
    assert rc == 0

    opencode_dir = repo / ".opencode" / "command"
    installed = sorted(p.name for p in opencode_dir.iterdir())
    assert installed == sorted(skills_install.PAYLOAD_NAMES)
    for name in skills_install.PAYLOAD_NAMES:
        source = (COMMANDS_DIR / name).read_text()
        opencode_content = (opencode_dir / name).read_text()
        # The documented transform, asserted (not just trusted): exactly what
        # `_opencode_frontmatter` produces from the SAME source.
        assert opencode_content == skills_install._opencode_frontmatter(source)

        src_front, src_body = source.split("---\n", 2)[1:]
        oc_front, oc_body = opencode_content.split("---\n", 2)[1:]
        assert oc_body == src_body                     # body, incl. `$1`, byte-identical
        assert "allowed-tools:" not in oc_front         # Claude-only -- no opencode equivalent
        assert "argument-hint:" not in oc_front         # Claude-only -- no opencode equivalent
        assert "description:" in oc_front               # the one line that DOES carry over
        assert "allowed-tools:" in src_front             # sanity: the source really has it


def test_install_repo_opencode_copy_idempotent_no_duplication(home, tmp_path):
    repo = tmp_path / "acme"
    _write_config(home, f'[repos.acme]\npath = "{repo}"\n'
                        '[maestro]\nrunner_enabled = ["claude", "opencode"]\n')

    assert cli_main(["--home", str(home), "install-commands", "--repo", "acme"]) == 0
    opencode_dir = repo / ".opencode" / "command"
    before = {p.name: (p.read_text(), p.stat().st_mtime) for p in opencode_dir.iterdir()}

    assert cli_main(["--home", str(home), "install-commands", "--repo", "acme"]) == 0
    after = {p.name: (p.read_text(), p.stat().st_mtime) for p in opencode_dir.iterdir()}
    assert before == after
    assert [p for p in opencode_dir.iterdir() if p.is_dir()] == []


def test_install_repo_is_idempotent_no_duplication(home, tmp_path):
    repo = tmp_path / "acme"
    _write_config(home, f'[repos.acme]\npath = "{repo}"\n')

    assert cli_main(["--home", str(home), "install-commands", "--repo", "acme"]) == 0
    commands_dir = repo / ".claude" / "commands"
    before = {p.name: (p.read_text(), p.stat().st_mtime) for p in commands_dir.iterdir()}

    assert cli_main(["--home", str(home), "install-commands", "--repo", "acme"]) == 0
    after = {p.name: (p.read_text(), p.stat().st_mtime) for p in commands_dir.iterdir()}
    assert before == after  # untouched -- not just same content, same mtime
    assert set(after) == set(skills_install.PAYLOAD_NAMES)
    assert [p for p in commands_dir.iterdir() if p.is_dir()] == []  # no nested/duplicated dirs


def test_install_repo_default_name_uses_legacy_repo_path(home, tmp_path):
    """A single-repo home (repo_path only, no [repos.*] tables) is reachable
    via the 'default' sentinel name -- matches repos.implicit_default()."""
    repo = tmp_path / "legacy"
    _write_config(home, f'[maestro]\nrepo_path = "{repo}"\n')

    assert cli_main(["--home", str(home), "install-commands", "--repo", "default"]) == 0
    commands_dir = repo / ".claude" / "commands"
    assert sorted(p.name for p in commands_dir.iterdir()) == sorted(skills_install.PAYLOAD_NAMES)


# ---------------------------------------------------------------------------
# AC2/AC3: --user symlinks all seven, repo untouched, idempotent
# ---------------------------------------------------------------------------

def test_install_user_symlinks_six_and_repo_working_tree_untouched(home, tmp_path, monkeypatch):
    repo = tmp_path / "bound-repo"
    _init_git_repo(repo)
    user_dir = tmp_path / "user-commands"
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(user_dir))
    _write_config(home, f'[maestro]\nrepo_path = "{repo}"\n')

    rc = cli_main(["--home", str(home), "install-commands", "--user"])
    assert rc == 0

    installed = sorted(p.name for p in user_dir.iterdir())
    assert installed == sorted(skills_install.PAYLOAD_NAMES)
    for name in skills_install.PAYLOAD_NAMES:
        dest = user_dir / name
        assert dest.is_symlink()
        assert dest.read_text() == (COMMANDS_DIR / name).read_text()

    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                         capture_output=True, text=True, check=True)
    assert out.stdout == ""


# ---------------------------------------------------------------------------
# OC-1 AC1: --user ALSO installs an opencode copy, from the same source, into
# opencode's own user-scope command directory -- a real (transformed) file,
# not a symlink, since its content is derived rather than identical.
# ---------------------------------------------------------------------------

def test_install_user_also_installs_opencode_copy(home, tmp_path, monkeypatch):
    repo = tmp_path / "bound-repo"
    _init_git_repo(repo)
    user_dir = tmp_path / "user-commands"
    opencode_user_dir = tmp_path / "opencode-user-commands"
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(user_dir))
    monkeypatch.setenv("MAESTRO_OPENCODE_COMMANDS_DIR", str(opencode_user_dir))
    _write_config(home, f'[maestro]\nrepo_path = "{repo}"\n'
                        'runner_enabled = ["claude", "opencode"]\n')

    rc = cli_main(["--home", str(home), "install-commands", "--user"])
    assert rc == 0

    installed = sorted(p.name for p in opencode_user_dir.iterdir())
    assert installed == sorted(skills_install.PAYLOAD_NAMES)
    for name in skills_install.PAYLOAD_NAMES:
        dest = opencode_user_dir / name
        assert not dest.is_symlink()   # derived content -- can't be a symlink to the source
        assert dest.read_text() == skills_install._opencode_frontmatter(
            (COMMANDS_DIR / name).read_text())

    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                         capture_output=True, text=True, check=True)
    assert out.stdout == ""    # repo untouched, same as the Claude side


def test_install_user_idempotent_leaves_correct_symlink_alone_and_repoints_stale(
        home, tmp_path, monkeypatch):
    user_dir = tmp_path / "user-commands"
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(user_dir))

    assert cli_main(["--home", str(home), "install-commands", "--user"]) == 0
    one = user_dir / skills_install.PAYLOAD_NAMES[0]
    correct_target = one.resolve()
    before_lstat = one.lstat()

    # A stale symlink for a second file -- points somewhere wrong.
    other = user_dir / skills_install.PAYLOAD_NAMES[1]
    other.unlink()
    stale_target = tmp_path / "elsewhere.md"
    stale_target.write_text("stale\n")
    other.symlink_to(stale_target)

    assert cli_main(["--home", str(home), "install-commands", "--user"]) == 0

    # untouched correct symlink -- same inode/mtime, never rewritten
    after_lstat = one.lstat()
    assert before_lstat.st_ino == after_lstat.st_ino
    assert before_lstat.st_mtime == after_lstat.st_mtime

    # stale symlink got repointed at the real payload (same payload dir as the
    # untouched file, different filename)
    assert other.resolve() == correct_target.parent / skills_install.PAYLOAD_NAMES[1]
    assert other.read_text() == (COMMANDS_DIR / skills_install.PAYLOAD_NAMES[1]).read_text()

    # no duplicated/nested directories
    assert [p for p in user_dir.iterdir() if p.is_dir()] == []


def test_install_user_refuses_to_clobber_a_human_file(home, tmp_path, monkeypatch):
    user_dir = tmp_path / "user-commands"
    user_dir.mkdir(parents=True)
    conflicting_name = skills_install.PAYLOAD_NAMES[0]
    (user_dir / conflicting_name).write_text("a human wrote this, not the verb\n")
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(user_dir))

    rc = cli_main(["--home", str(home), "install-commands", "--user"])
    assert rc != 0
    # the human's file survives untouched, and nothing else got written either
    assert (user_dir / conflicting_name).read_text() == "a human wrote this, not the verb\n"
    assert not (user_dir / skills_install.PAYLOAD_NAMES[1]).exists()


# ---------------------------------------------------------------------------
# AC4: unknown --repo <name> fails clean, lists configured names, writes nothing
# ---------------------------------------------------------------------------

def test_unknown_repo_name_exits_nonzero_lists_configured_names_writes_nothing(
        home, tmp_path, capsys):
    repo = tmp_path / "acme"
    _write_config(home, f'[repos.acme]\npath = "{repo}"\n')

    rc = cli_main(["--home", str(home), "install-commands", "--repo", "typo-name"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "acme" in err
    assert not repo.exists()


# ---------------------------------------------------------------------------
# AC5: the payload ships in the wheel and resolves from the installed package
# ---------------------------------------------------------------------------

def test_wheel_build_target_covers_all_six_payload_files():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    packages = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert "maestro" in packages


# ---------------------------------------------------------------------------
# T-91: the opencode payload is gated on "opencode" in cfg.runner_enabled --
# a runner nobody enabled gets no global config written for it. Default
# runner_enabled is claude-only (config.py), so a home with no config.toml
# `runner_enabled` line at all is exactly the gated case below.
# ---------------------------------------------------------------------------

def test_gated_install_user_creates_nothing_under_opencode_user_scope(home):
    """AC1: over a temp home whose config has no `runner_enabled` line
    (default claude-only), --user still installs all 7 Claude symlinks, but
    creates NOTHING under the opencode user scope -- not even the dirs."""
    assert cli_main(["--home", str(home), "install-commands", "--user"]) == 0

    cfg = Config(home=home)
    commands_dir = skills_install.user_commands_dir(cfg)
    assert sorted(p.name for p in commands_dir.iterdir()) == sorted(skills_install.PAYLOAD_NAMES)
    assert not skills_install.opencode_user_config_path(cfg).exists()
    assert not skills_install.opencode_user_commands_dir(cfg).exists()
    assert not skills_install.opencode_user_agent_dir(cfg).exists()


def test_gated_install_repo_creates_nothing_under_dot_opencode(home, tmp_path):
    """AC2: same gate, --repo target -- `<repo>/.opencode` doesn't exist
    afterwards and `git status --porcelain` lists only `.claude/commands/`
    additions."""
    repo = tmp_path / "acme"
    _init_git_repo(repo)
    _write_config(home, f'[repos.acme]\npath = "{repo}"\n')

    assert cli_main(["--home", str(home), "install-commands", "--repo", "acme"]) == 0

    assert not (repo / ".opencode").exists()
    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                         capture_output=True, text=True, check=True)
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    assert lines  # the Claude-side additions are still there
    # git reports a wholly-untracked dir as one line for the dir itself, not
    # per-file (`?? .claude/`) -- assert on that, and that .opencode never
    # shows up at all (it wasn't created, so git has nothing to report there).
    assert all(".claude" in line for line in lines)
    assert not any(".opencode" in line for line in lines)


def test_all_runners_opt_in_installs_full_opencode_payload_on_claude_only_board(home, tmp_path):
    """AC5: `--all-runners` forces the opencode payload even though
    `runner_enabled` admits only claude."""
    repo = tmp_path / "acme"
    _write_config(home, f'[repos.acme]\npath = "{repo}"\n')

    rc = cli_main(["--home", str(home), "install-commands", "--repo", "acme", "--all-runners"])
    assert rc == 0

    opencode_dir = repo / ".opencode" / "command"
    assert sorted(p.name for p in opencode_dir.iterdir()) == sorted(skills_install.PAYLOAD_NAMES)
    assert (repo / ".opencode" / "opencode.jsonc").exists()
    assert sorted(p.name for p in (repo / ".opencode" / "agent").iterdir()) == \
        sorted(skills_install.PAYLOAD_NAMES)


def test_all_runners_flag_documented_in_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_main(["install-commands", "--help"])
    assert exc.value.code == 0
    assert "--all-runners" in capsys.readouterr().out


def test_gated_failed_user_install_leaves_no_opencode_directories_behind(
        home, tmp_path, monkeypatch):
    """AC6: the refuse-to-clobber path (a human-owned real file at a Claude
    payload path) leaves no opencode directories behind in the user scope on
    a claude-only board -- they're never even `mkdir`'d when the gate is
    closed, regardless of when the conflict is discovered."""
    user_dir = tmp_path / "user-commands"
    user_dir.mkdir(parents=True)
    conflicting_name = skills_install.PAYLOAD_NAMES[0]
    (user_dir / conflicting_name).write_text("a human wrote this, not the verb\n")
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(user_dir))

    rc = cli_main(["--home", str(home), "install-commands", "--user"])
    assert rc != 0

    cfg = Config(home=home)
    assert not skills_install.opencode_user_commands_dir(cfg).exists()
    assert not skills_install.opencode_user_agent_dir(cfg).exists()
    assert not skills_install.opencode_user_config_path(cfg).exists()

    payload_dir = REPO_ROOT / "maestro" / "_skill_commands"
    names = sorted(p.name for p in payload_dir.iterdir())
    assert names == sorted(skills_install.PAYLOAD_NAMES)
    # every payload entry resolves to real, non-empty content (proves the
    # symlinks aren't dangling) -- no build, no network.
    for name in skills_install.PAYLOAD_NAMES:
        assert (payload_dir / name).read_text() == (COMMANDS_DIR / name).read_text()


def test_payload_resolves_from_installed_package_directory_alone(tmp_path):
    """Copy ONLY the installed package directory (no repo root, no `.claude/`)
    into a tmp dir and confirm payload resolution still finds all seven there --
    proves resolution doesn't depend on a repo-root-relative path."""
    pkg_dir = Path(importlib.resources.files("maestro"))

    copy_root = tmp_path / "installed-only"
    shutil.copytree(pkg_dir, copy_root, symlinks=False)  # dereference, like a real wheel install

    payload = copy_root / "_skill_commands"
    assert payload.is_dir()
    names = sorted(p.name for p in payload.iterdir())
    assert names == sorted(skills_install.PAYLOAD_NAMES)
    for name in skills_install.PAYLOAD_NAMES:
        content = (payload / name).read_text()
        assert content == (COMMANDS_DIR / name).read_text()
        assert content.strip() != ""


# ---------------------------------------------------------------------------
# AC6/AC7: doctor treats a user-scope install as satisfying missing_reconcile_skill,
# and the check stays machine-independent (MAESTRO_USER_COMMANDS_DIR override)
# ---------------------------------------------------------------------------

def test_doctor_cli_reports_ok_when_user_scope_install_satisfies_check(
        home, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "no-commands-repo"
    repo.mkdir()
    user_dir = tmp_path / "user-commands"
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(user_dir))

    _write_config(home, f'[maestro]\nrepo_path = "{repo}"\nmin_spawn_interval = 0\n')
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: 0\n")
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "T-1", "spec_hash": disp.spec_hash_on_disk(home, "T-1")}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    # not installed anywhere yet -> warn
    assert cli_main(["--home", str(home), "doctor"]) == 0
    warn_check = next(c for c in json.loads(capsys.readouterr().out)["checks"]
                      if c["name"] == "missing_reconcile_skill")
    assert warn_check["status"] == "warn"
    assert "default" in warn_check["missing"]

    # install into the user dir (no --repo write to `repo` at all)
    assert cli_main(["--home", str(home), "install-commands", "--user"]) == 0
    assert not (repo / ".claude").exists()
    capsys.readouterr()  # discard install-commands' own JSON output

    assert cli_main(["--home", str(home), "doctor"]) == 0
    ok_check = next(c for c in json.loads(capsys.readouterr().out)["checks"]
                    if c["name"] == "missing_reconcile_skill")
    assert ok_check["status"] == "ok"
    assert ok_check["missing"] == []


def test_doctor_cli_reports_ok_when_repo_scope_install_satisfies_check(
        home, tmp_path, monkeypatch, capsys):
    """T-87 AC8's `--repo` counterpart: a single real `install-commands --repo`
    run flips the check from warn to ok in one step -- the completeness rule
    and the installer agree on the same `PAYLOAD_NAMES` set (a stub loop that
    only wrote a subset would have left this warning)."""
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(tmp_path / "no-user-commands"))
    repo = tmp_path / "acme"
    _init_git_repo(repo)
    _write_config(home, f'[maestro]\nmin_spawn_interval = 0\n\n'
                        f'[repos.acme]\npath = "{repo}"\ndefault = true\n')
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: 0\n")
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "T-1", "spec_hash": disp.spec_hash_on_disk(home, "T-1")}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    assert cli_main(["--home", str(home), "doctor"]) == 0
    warn_check = next(c for c in json.loads(capsys.readouterr().out)["checks"]
                      if c["name"] == "missing_reconcile_skill")
    assert warn_check["status"] == "warn"

    assert cli_main(["--home", str(home), "install-commands", "--repo", "acme"]) == 0
    capsys.readouterr()  # discard install-commands' own JSON output

    assert cli_main(["--home", str(home), "doctor"]) == 0
    ok_check = next(c for c in json.loads(capsys.readouterr().out)["checks"]
                    if c["name"] == "missing_reconcile_skill")
    assert ok_check["status"] == "ok"
    assert ok_check["missing"] == []
