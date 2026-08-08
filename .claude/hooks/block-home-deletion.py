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

This is a textual heuristic over the raw command string, not a real shell
parser or interpreter -- it does not evaluate command substitution
(``$(...)``, backticks), does not track pipelines across clauses (e.g. a
path introduced by ``find`` and consumed by a piped ``xargs rm``), and does
not expand globs beyond a trailing ``*``/``**`` wildcard. Closing those
would need an actual shell AST parser or executing in a dry-run sandbox, a
different (and much heavier) design than a stdlib-only regex guard. The
threat model this guards against is a well-intentioned agent accidentally
destroying the sole source of truth while following bad instructions or
cleaning up a workspace (see CLAUDE.md and the 2026-07-18 incident) -- not
a determined adversary deliberately obfuscating a command to evade this
hook. Within that scope it deliberately errs toward catching more than a
real parser would, since a false positive just costs a retry while a false
negative is unrecoverable.
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
# A trailing bare wildcard ("*", "**", "foo/*", "foo/**") -- stripped before
# resolving a candidate path, see _strip_trailing_glob.
_TRAILING_GLOB_RE = re.compile(r"/\*+$")
_BARE_GLOB_RE = re.compile(r"^\*+$")

# Split a compound command on shell control operators so each clause is
# checked independently (`cd /tmp && rm -rf $MAESTRO_HOME` must still be
# caught even though the leading clause is harmless).
_CLAUSE_SPLIT_RE = re.compile(r"&&|\|\||;|\|")


def _expand_vars(command: str, home: Path) -> str:
    """Textually substitute $MAESTRO_HOME/$MHOME/$HOME/~ so path matching
    works even when the command references the home via a shell variable
    rather than a literal path."""
    command = re.sub(r"\$\{MAESTRO_HOME\}|\$MAESTRO_HOME\b", str(home), command)
    command = re.sub(r"\$\{MHOME\}|\$MHOME\b", str(home), command)
    home_dir = str(Path.home())
    command = re.sub(r"\$\{HOME\}|\$HOME\b", home_dir, command)
    command = re.sub(r"(?<![\w])~(?=/|\s|$)", home_dir, command)
    return command


def _strip_trailing_glob(path_str: str) -> str:
    """A shell expands a trailing wildcard to "everything in that directory"
    -- `rm -rf $MAESTRO_HOME/*` deletes events/tickets/inbox/config.toml just
    as thoroughly as `rm -rf $MAESTRO_HOME` does, even though the literal
    string never equals the home path. Strip a bare `*`/`**` down to "." (the
    implicit-cwd case) and a path-qualified trailing wildcard
    (`<dir>/*`, `<dir>/**`) down to `<dir>`, so both resolve and get checked
    exactly like a direct reference to that directory."""
    if _BARE_GLOB_RE.match(path_str):
        return "."
    stripped = _TRAILING_GLOB_RE.sub("", path_str)
    return stripped or "/"


def _candidate_paths(clause: str, *, skip_leading: str | None = None) -> list[str]:
    """Best-effort extraction of path-looking tokens from a shell clause.
    Heuristic, not a real shell parser -- deliberately erring toward
    catching more than a real parser would, since a false positive just
    costs a retry while a false negative is unrecoverable.

    ``skip_leading`` drops the clause's first token when it is exactly the
    matched risky verb (e.g. "rm" in "rm -rf") -- otherwise the verb word
    itself ends up in the candidate list, which for a *bare* `rm -rf` (no
    real path argument) makes the fallback "no path token -> check cwd"
    branch below never fire (the verb token makes the list non-empty), so a
    bare destructive command run from inside a protected directory was
    silently allowed through. Only the leading occurrence is dropped, so a
    literal argument that happens to equal the verb name (`mv rm newname`)
    is still checked.
    """
    tokens = re.split(r"\s+", clause.strip())
    if skip_leading and tokens and tokens[0] == skip_leading:
        tokens = tokens[1:]
    out = []
    for tok in tokens:
        tok = tok.strip("'\"")
        if not tok or tok.startswith("-"):
            continue
        out.append(tok)
    return out


def _is_protected(path_str: str, cwd: Path, home: Path) -> bool:
    path_str = _strip_trailing_glob(path_str)
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
        # `rm -rf $MAESTRO_HOME`, or `rm -rf $MAESTRO_HOME/*` once stripped
        # above). Reconcilers always run with a cwd nested under MAESTRO_HOME
        # (worktrees live at home/worktrees/<KEY>), so `root in p.parents`
        # here would make *every* relative path anywhere in a reconciler's
        # own worktree "protected" -- home is trivially an ancestor of any
        # path resolved against a cwd that is itself under home. That
        # false-positived nearly every ordinary Bash call (rm of a build dir,
        # mv, a plain `>` redirect) once this hook was wired in. The named
        # subtrees (events/tickets/inbox) are specific enough that matching
        # anything nested inside them is exactly the intent.
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
        paths = _candidate_paths(clause, skip_leading=risky_verb.group(1)) if risky_verb else []
        paths += _redirect_targets(clause)
        for p in paths:
            if _is_protected(p, cwd, home):
                return f"{clause!r} targets protected MAESTRO_HOME path {p!r}"
        # A bare `rm`/`mv`/`truncate` with no real path token -- e.g. `rm -rf`
        # or `rm -rf *` (a glob has nothing to resolve to) -- implicitly acts
        # on the cwd, so check that too.
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
