"""GA-7: a fresh `git worktree add` brings tracked files only, so a reconciler's
worktree silently lacks the repo's gitignored guidance (`CLAUDE.local.md`,
`.claude/settings.local.json`) and any installed dependency tree -- for a JS repo
whose `node_modules` is multiple GB, that means the implementing reconciler can't
even run the test command. `maestro-reconcile-ready.md`'s `MODE == git` branch now
primes a fresh worktree with a real, write-isolated `cp` copy (never a symlink --
a symlink would share ONE mutable tree across every concurrent worktree) of each,
after excluding the copied names from `git add` via the git-common `info/exclude`
file (idempotently, since that file is shared across every worktree of the repo).

These tests: (1)/(2) static checks over the prime block's text (no symlink, the
cp ladder, the git-common exclude path, idempotent guards), (3)/(4)/(5) extract
that block verbatim and run it against a real throwaway repo + linked worktree,
proving it's clean (git add -A stages nothing), write-isolated (mutating the
worktree's copy never touches the source), and idempotent (a second run doesn't
duplicate exclude entries or nest node_modules), (6) every command word the block
invokes has a matching grant in `.claude/settings.json`, and (7) a real dispatcher
sweep resolves a ready ticket to this file and the block primes the exact worktree
path the dispatcher hands a real reconciler.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from maestro import config as config_mod
from maestro import snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.sessions import DryRunSessions

from conftest import git as _git, make_origin_and_repo as _make_origin_and_repo
from test_reconcile_skill import COMMANDS_DIR, _commands_path, _skills_path, _strip_frontmatter

READY_TEXT = _strip_frontmatter(_commands_path("ready").read_text())


def _ready_git_block(text: str) -> str:
    """The `MODE == git` bash fence -- fetch, worktree add/adopt, the prime
    block and the final set-phase -- extracted verbatim."""
    m = re.search(r"\*\*`MODE == git`\*\*.*?```bash\n(.*?)```", text, re.DOTALL)
    assert m, "could not find the MODE == git bash block"
    return m.group(1)


def _prime_block(git_block: str) -> str:
    """Just the priming portion -- from the WT= assignment through the
    node_modules ladder -- excluding the worktree add/adopt pair before it and
    the final set-phase after it, so it can run against an already-existing
    worktree without needing a live $REPO/$BASE to fetch from."""
    start = git_block.index('WT="$MHOME')
    end = git_block.index("maestro set-phase")
    return git_block[start:end]


def _run_bash(script: str, *, cwd: Path, env_overrides: dict) -> subprocess.CompletedProcess:
    script_file = cwd / "prime.sh"
    script_file.write_text(script, encoding="utf-8")
    env = {**os.environ, **env_overrides}
    return subprocess.run(["bash", str(script_file)], cwd=cwd, env=env,
                          capture_output=True, text=True)


# ---------------------------------------------------------------------------
# AC1: no symlink, replaced by the cp ladder; also copies CLAUDE.local.md and
# .claude/settings.local.json; the whole thing sits inside MODE == git only
# ---------------------------------------------------------------------------

def test_no_symlink_and_cp_ladder_guarded_on_both_conditions():
    for path in (_commands_path("ready"), _skills_path("ready")):
        text = _strip_frontmatter(path.read_text())
        assert "ln -s" not in text, f"{path}: node_modules symlink is still present"

        git_block = _ready_git_block(text)
        assert 'cp -c -R' in git_block and 'cp -al' in git_block and 'cp -R' in git_block
        assert git_block.count(" || cp ") + git_block.count("\n    || cp ") >= 1 or \
            re.search(r"cp -c -R.*?\|\|.*?cp -al.*?\|\|.*?cp -R", git_block, re.DOTALL), \
            f"{path}: cp -c -R / cp -al / cp -R don't read as one ladder"
        assert '[ -d "$REPO/node_modules" ]' in git_block
        assert '[ ! -e "$WT/node_modules" ]' in git_block

        assert 'CLAUDE.local.md' in git_block
        assert '.claude/settings.local.json' in git_block

        # None of this leaks into the MODE == local branch just above it.
        local_start = text.index("MODE == local")
        git_start = text.index("MODE == git")
        local_section = text[local_start:git_start]
        assert "node_modules" not in local_section
        assert "CLAUDE.local.md" not in local_section


# ---------------------------------------------------------------------------
# AC2: the exclude append is resolved via --git-path (never the literal
# linked-worktree .git/info/exclude), and is idempotently guarded
# ---------------------------------------------------------------------------

def test_exclude_append_uses_git_common_path_and_is_idempotently_guarded():
    for path in (_commands_path("ready"), _skills_path("ready")):
        git_block = _ready_git_block(_strip_frontmatter(path.read_text()))
        assert 'rev-parse --git-path info/exclude' in git_block
        assert '$WT/.git/info/exclude' not in git_block, \
            f"{path}: still references the literal linked-worktree path, which is a FILE"
        # The append only happens if the name isn't already there.
        assert 'grep -qxF' in git_block


# ---------------------------------------------------------------------------
# AC3/AC4/AC5: extract the prime block verbatim and run it for real -- clean,
# write-isolated, and idempotent, against a real throwaway repo + linked
# worktree ("the same test" per the spec, so one function proves all three).
# ---------------------------------------------------------------------------

def test_prime_block_is_clean_isolated_and_idempotent(tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path, name="target")

    # Untracked, gitignored-by-convention source-side files a human's real checkout
    # would carry -- present, but never committed.
    (repo / "CLAUDE.local.md").write_text("private notes\n", encoding="utf-8")
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "settings.local.json").write_text('{"allow": []}\n', encoding="utf-8")
    nm_file = repo / "node_modules" / "pkgA" / "file.txt"
    nm_file.parent.mkdir(parents=True)
    nm_file.write_text("original\n", encoding="utf-8")

    mhome = tmp_path / "mhome"
    wt = mhome / "worktrees" / "G-1"
    wt.parent.mkdir(parents=True)
    _git("worktree", "add", "-q", "-b", "maestro/G-1", str(wt), "main", cwd=repo)

    prime = _prime_block(_ready_git_block(READY_TEXT))
    env = {"REPO": str(repo), "MHOME": str(mhome), "KEY": "G-1"}

    # --- AC3: running it leaves the worktree clean -- git add -A stages nothing ---
    result = _run_bash(prime, cwd=tmp_path, env_overrides=env)
    assert result.returncode == 0, result.stderr

    status = subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                            capture_output=True, text=True, check=True)
    assert status.stdout == "", f"worktree not clean after priming: {status.stdout}"
    _git("add", "-A", cwd=wt)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=wt,
                            capture_output=True, text=True, check=True)
    assert staged.stdout == "", f"primed files got staged: {staged.stdout}"

    assert (wt / "CLAUDE.local.md").read_text(encoding="utf-8") == "private notes\n"
    assert (wt / ".claude" / "settings.local.json").read_text(encoding="utf-8") == '{"allow": []}\n'
    assert (wt / "node_modules" / "pkgA" / "file.txt").read_text(encoding="utf-8") == "original\n"

    # --- AC4: write isolation -- mutating the worktree's copy never reaches $REPO ---
    (wt / "node_modules" / "pkgA" / "file.txt").write_text("mutated\n", encoding="utf-8")
    assert nm_file.read_text(encoding="utf-8") == "original\n", \
        "mutating $WT/node_modules leaked into $REPO/node_modules -- not write-isolated " \
        "(a symlink, or a hardlink mutated in place, would fail exactly this way)"

    # --- AC5: idempotence -- a second run is clean, doesn't duplicate excludes,
    # and doesn't nest a second node_modules inside the first ---
    result2 = _run_bash(prime, cwd=tmp_path, env_overrides=env)
    assert result2.returncode == 0, result2.stderr

    exc = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-path", "info/exclude"],
                         capture_output=True, text=True, check=True).stdout.strip()
    exclude_lines = Path(exc).read_text(encoding="utf-8").splitlines()
    for name in ("CLAUDE.local.md", ".claude/settings.local.json", "node_modules/"):
        assert exclude_lines.count(name) == 1, \
            f"{name} appears {exclude_lines.count(name)} times in info/exclude after 2 runs"

    assert not (wt / "node_modules" / "node_modules").exists(), \
        "second run nested a node_modules inside node_modules"


# ---------------------------------------------------------------------------
# AC6: every command word the prime block invokes has a matching
# Bash(<word>:*) grant in .claude/settings.json
# ---------------------------------------------------------------------------

_SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "for", "do", "done", "in", "[", "]"}


def _line_command_words(line: str) -> list[str]:
    """The external command word(s) a single line of the prime block invokes --
    both its own leading word and any embedded in a `VAR="$(...)"` command
    substitution -- skipping comments, shell keywords, and bare assignments."""
    line = line.strip()
    if not line or line.startswith("#"):
        return []
    line = line.rstrip("\\").strip()
    if not line:
        return []
    if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', line):
        # A variable assignment -- only a `$(...)` command substitution inside it
        # is an actual external invocation; the rest is shell syntax, not a command.
        return [m.group(1) for m in re.finditer(r"\$\(\s*([A-Za-z0-9_./]+)", line)]
    words = []
    for part in re.split(r"\|\||&&", line):
        part = part.strip().lstrip("|&").strip()
        if not part:
            continue
        first = part.split()[0]
        if first not in _SHELL_KEYWORDS:
            words.append(first)
    return words


def test_settings_json_grants_every_command_word_in_prime_block():
    import json
    settings = json.loads((COMMANDS_DIR.parent / "settings.json").read_text())
    allowed = set()
    for entry in settings["permissions"]["allow"]:
        m = re.match(r"Bash\(([^:]+):\*\)$", entry)
        if m:
            allowed.add(m.group(1))

    for path in (_commands_path("ready"), _skills_path("ready")):
        git_block = _ready_git_block(_strip_frontmatter(path.read_text()))
        words = {w for line in git_block.splitlines() for w in _line_command_words(line)}
        assert "cp" in words, f"{path}: expected the block to actually invoke cp"
        for word in words:
            assert word in allowed, (
                f"{path}: prime block invokes {word!r} with no Bash({word}:*) grant "
                f"in .claude/settings.json -- an unattended reconciler would stall on it")


# ---------------------------------------------------------------------------
# AC7: real-surface QA -- a real dispatcher sweep routes a ready ticket to this
# file, and the prime block primes the exact worktree path the dispatcher hands
# a real reconciler (store.worktree_path). Only the claude spawn is mocked.
# ---------------------------------------------------------------------------

def test_ready_dispatch_sweep_primes_the_dispatcher_resolved_worktree(home, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path, name="dispatch-target")
    (repo / "CLAUDE.local.md").write_text("private notes\n", encoding="utf-8")
    nm_file = repo / "node_modules" / "pkgA" / "file.txt"
    nm_file.parent.mkdir(parents=True)
    nm_file.write_text("original\n", encoding="utf-8")

    (home / "config.toml").write_text(f'[maestro]\nrepo_path = "{repo}"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))

    assert cli_main(["--home", str(home), "create", "GA-7 QA ticket",
                     "--key", "Q-1", "--no-nudge"]) == 0
    from maestro.dispatcher import dispatch
    dispatch(cfg, DryRunSessions(), now=1000)   # mints the ticket (starts in triaging)

    assert cli_main(["--home", str(home), "set-phase", "Q-1", "ready",
                     "--reason", "test: ready for worktree setup"]) == 0

    sessions = DryRunSessions()
    dispatch(cfg, sessions, now=2000)
    spawned = {key: prompt for key, prompt, *_ in sessions.spawned}
    assert spawned.get("Q-1") == "/maestro-reconcile-ready Q-1", (
        f"ready ticket should route to the ready command, spawned: {spawned}")

    # The dispatcher never actually runs the reconciler (DryRunSessions) -- run the
    # real MODE == git block ourselves, at the exact path the dispatcher would hand
    # a real reconciler, and drive the resulting phase transition through the real CLI.
    wt = store.worktree_path(home, "Q-1")
    _git("worktree", "add", "-q", "-b", "maestro/Q-1", str(wt), "main", cwd=repo)

    git_block = _ready_git_block(READY_TEXT)
    env = {"REPO": str(repo), "MHOME": str(home), "KEY": "Q-1", "PREFIX": "maestro/",
           "BASE": "main", "MAESTRO_HOME": str(home)}
    script_file = home / "run_ready_block.sh"
    script_file.write_text(git_block, encoding="utf-8")
    result = subprocess.run(["bash", str(script_file)], cwd=home,
                            env={**os.environ, **env}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    assert (wt / "CLAUDE.local.md").read_text(encoding="utf-8") == "private notes\n"
    assert (wt / "node_modules" / "pkgA" / "file.txt").read_text(encoding="utf-8") == "original\n"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                            capture_output=True, text=True, check=True)
    assert status.stdout == ""

    snap = snap_mod.load(home, "Q-1")
    assert snap.phase == "implementing", \
        f"the block's own `maestro set-phase implementing` should have landed, got {snap.phase!r}"
