#!/usr/bin/env python3
"""PreToolUse hook: block destructive shell commands against MAESTRO_HOME.

Reads a Claude Code PreToolUse hook payload from stdin. For ``Bash`` tool
calls, blocks (exit 2) commands that would delete, move, truncate, or clean
files under the resolved MAESTRO_HOME's ``events/``, ``tickets/``,
``inbox/``, or ``config.toml`` -- the sole source of truth for every
ticket's history (see CLAUDE.md: "NEVER delete the state home / event
logs" -- this is exactly how the dogfood board was lost on 2026-07-18).
Everything else is allowed (exit 0). stdlib-only, no external dependency
(not even the ``maestro`` package), so it keeps working even if the venv
is broken or not installed.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Same resolution order as maestro.store.resolve_home (env > default).
# Duplicated rather than imported so this hook has zero dependency on the
# maestro package being importable by whatever python3 runs the hook.
def resolve_home() -> Path:
    raw = os.environ.get("MAESTRO_HOME") or "~/.maestro"
    return Path(raw).expanduser().resolve()


# Sub-paths (relative to MAESTRO_HOME) that are the irreplaceable source of
# truth. "" protects the home root itself -- a bare `rm -rf $MAESTRO_HOME`
# takes all of these with it.
_PROTECTED_RELATIVE = ["", "events", "tickets", "inbox", "config.toml"]

# Verbs/operators that can destroy or corrupt files. The lookbehind excludes
# \w/./- so this doesn't fire inside an unrelated word ("confirm", "warmup",
# "backup.rm", a "-rm" flag) -- but deliberately does NOT exclude "/", so an
# absolute or relative binary invocation (`/bin/rm ...`, `bin/rm ...`) still
# matches: excluding "/" here originally let `/bin/rm -rf $MAESTRO_HOME`
# and similar path-qualified invocations (common to bypass a `rm` shell
# alias) slip through undetected.
_RISKY_VERB_RE = re.compile(r"(?<![\w.-])(rm|mv|truncate)(?![\w./-])")
_GIT_CLEAN_RE = re.compile(r"\bgit\s+clean\b")
# A single `>` that is not part of `>>` (append) or `>&`/fd-duplication --
# i.e. a truncating write.
_REDIRECT_RE = re.compile(r"(?<!>)>(?!>|&)")

# Split a compound command on shell control operators so each clause is
# checked independently (`cd /tmp && rm -rf $MAESTRO_HOME` must still be
# caught even though the leading clause is harmless).
_CLAUSE_SPLIT_RE = re.compile(r"&&|\|\||;|\|")


def _expand_vars(command: str, home: Path) -> str:
    """Textually substitute $MAESTRO_HOME/$HOME/~ so path matching works
    even when the command references the home via a shell variable rather
    than a literal path."""
    command = re.sub(r"\$\{MAESTRO_HOME\}|\$MAESTRO_HOME\b", str(home), command)
    home_dir = str(Path.home())
    command = re.sub(r"\$\{HOME\}|\$HOME\b", home_dir, command)
    command = re.sub(r"(?<![\w])~(?=/|\s|$)", home_dir, command)
    return command


def _candidate_paths(clause: str) -> list[str]:
    """Best-effort extraction of path-looking tokens from a shell clause.
    Heuristic, not a real shell parser -- deliberately erring toward
    catching more than a real parser would, since a false positive just
    costs a retry while a false negative is unrecoverable."""
    tokens = re.split(r"\s+", clause.strip())
    out = []
    for tok in tokens:
        tok = tok.strip("'\"")
        if not tok or tok.startswith("-"):
            continue
        out.append(tok)
    return out


def _is_protected(path_str: str, cwd: Path, home: Path) -> bool:
    try:
        p = Path(path_str)
        p = p.resolve() if p.is_absolute() else (cwd / p).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for rel in _PROTECTED_RELATIVE:
        root = home / rel if rel else home
        try:
            root = root.resolve()
        except (OSError, RuntimeError):
            continue
        if p == root:
            return True
        # For the home root itself ("") only an *exact* match counts (a bare
        # `rm -rf $MAESTRO_HOME`). Reconcilers always run with a cwd nested
        # under MAESTRO_HOME (worktrees live at home/worktrees/<KEY>), so
        # `root in p.parents` here would make *every* relative path anywhere
        # in a reconciler's own worktree "protected" -- home is trivially an
        # ancestor of any path resolved against a cwd that is itself under
        # home. That false-positived nearly every ordinary Bash call (rm of a
        # build dir, mv, a plain `>` redirect) once this hook was wired in.
        # The named subtrees (events/tickets/inbox) are specific enough that
        # matching anything nested inside them is exactly the intent.
        if rel and root in p.parents:
            return True
    return False


def _redirect_targets(clause: str) -> list[str]:
    """Extract destination path(s) following a truncating `>` redirect."""
    targets = []
    for m in _REDIRECT_RE.finditer(clause):
        rest = clause[m.end():].strip()
        if not rest:
            continue
        targets.append(rest.split()[0].strip("'\""))
    return targets


def check_command(command: str, cwd: Path, home: Path) -> str | None:
    """Return a block reason if *command* is destructive against *home*, else None."""
    command = _expand_vars(command, home)
    for clause in _CLAUSE_SPLIT_RE.split(command):
        clause = clause.strip()
        if not clause:
            continue

        # `git clean` acts on the cwd's working tree by default, and can also
        # take an explicit pathspec -- check both, ignoring the "git"/"clean"
        # tokens themselves so the block message names a real path.
        if _GIT_CLEAN_RE.search(clause):
            if _is_protected(".", cwd, home):
                return f"{clause!r} would run inside protected MAESTRO_HOME path {cwd}"
            for p in _candidate_paths(clause):
                if p in ("git", "clean"):
                    continue
                if _is_protected(p, cwd, home):
                    return f"{clause!r} targets protected MAESTRO_HOME path {p!r}"
            continue

        risky_verb = _RISKY_VERB_RE.search(clause)
        paths = _candidate_paths(clause) if risky_verb else []
        paths += _redirect_targets(clause)
        for p in paths:
            if _is_protected(p, cwd, home):
                return f"{clause!r} targets protected MAESTRO_HOME path {p!r}"
        # A bare `rm`/`mv`/`truncate` with no path token (unusual, but cheap
        # to cover) implicitly acts on the cwd -- check that too.
        if risky_verb and not paths and _is_protected(".", cwd, home):
            return f"{clause!r} would act on protected MAESTRO_HOME path {cwd}"
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # can't parse -- fail open, don't break unrelated tool calls

    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command")
    if not command or not isinstance(command, str):
        return 0

    cwd_raw = payload.get("cwd") or os.getcwd()
    try:
        cwd = Path(cwd_raw).resolve()
    except (OSError, RuntimeError):
        cwd = Path.cwd()

    home = resolve_home()
    reason = check_command(command, cwd, home)
    if reason:
        print(
            f"BLOCKED by block-home-deletion hook: {reason}\n"
            "MAESTRO_HOME's events/tickets/inbox/config.toml are the sole "
            "source of truth and have no other copy -- deleting them is "
            "unrecoverable (see CLAUDE.md). If you genuinely need to reset "
            "this home, run `maestro backup` first and get the human's "
            "explicit, in-the-moment go-ahead.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
