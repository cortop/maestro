"""The destructive-command predicate: is *command* destructive against MAESTRO_HOME?

Extracted from ``block-home-deletion.py`` (T-34/RF-5) so the same core is importable
by more than one adapter: the Claude Code ``PreToolUse`` hook (JSON-on-stdin) and a
plain-argv adapter for non-Claude runners (see ``guard_argv.py``). This module is the
single source of truth for the protected-path list and the destructive-verb/redirect
heuristics -- neither adapter, nor anything in ``maestro/`` that needs to describe the
same guard to a third-party runner's own permission config, may keep a second copy.

stdlib-only, no dependency on the ``maestro`` package (not even ``store.resolve_home``,
which this duplicates) -- see ``block-home-deletion.py``'s module docstring for why:
every adapter that imports this module must keep working even if the venv is broken
or not installed.

This is a textual heuristic over a raw command string, not a real shell parser or
interpreter -- it does not evaluate command substitution (``$(...)``, backticks), does
not track pipelines across clauses (e.g. a path introduced by ``find`` and consumed by
a piped ``xargs rm``), and does not expand globs beyond a trailing ``*``/``**``
wildcard. Closing those would need an actual shell AST parser or executing in a
dry-run sandbox, a different (and much heavier) design than a stdlib-only regex guard.
The threat model this guards against is a well-intentioned agent accidentally
destroying the sole source of truth while following bad instructions or cleaning up a
workspace (see CLAUDE.md and the 2026-07-18 incident) -- not a determined adversary
deliberately obfuscating a command to evade this hook. Within that scope it
deliberately errs toward catching more than a real parser would, since a false
positive just costs a retry while a false negative is unrecoverable.

T-145: the guard used to protect only the single resolved MAESTRO_HOME. It now
protects a *set* of board roots -- the resolved home, the default home (when it
itself looks like a board), and each of the default home's board-like immediate
children -- plus every ancestor of every such root (a bare deletion of the default
home's parent, or of the real user home, takes every board under it with it). See
``protected_roots`` and ``_is_protected`` below.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


# Same resolution order as maestro.store.resolve_home (env > default).
# Duplicated rather than imported so every adapter of this module has zero
# dependency on the maestro package being importable by whatever python3 runs it.
def resolve_home() -> Path:
    raw = os.environ.get("MAESTRO_HOME") or "~/.maestro"
    return Path(raw).expanduser().resolve()


# Sub-paths (relative to a board root) that are the irreplaceable source of
# truth. "" protects the root itself, and (see _is_protected) every ancestor
# of it -- a bare deletion or move of a board root's parent, all the way up
# to the user's own home directory, takes every one of these with it too.
PROTECTED_RELATIVE = ["", "events", "tickets", "inbox", "config.toml"]

# The default home the guard falls back to when MAESTRO_HOME is unset -- also
# the directory whose immediate children get scanned for sibling boards (see
# protected_roots).
_DEFAULT_HOME_RAW = "~/.maestro"


def _looks_like_board(path: Path) -> bool:
    """True when *path* has the shape of a maestro home: an events/ dir
    together with tickets/ or config.toml. A bare events/+inbox/ phantom
    home (no tickets/, no config.toml) does not count on its own -- but the
    resolved MAESTRO_HOME is always in protected_roots's result regardless,
    so that phantom stays covered either way."""
    try:
        if not (path / "events").is_dir():
            return False
        return (path / "tickets").is_dir() or (path / "config.toml").is_file()
    except OSError:
        return False


def protected_roots(home: Path) -> list[Path]:
    """Every board root this guard protects: *home* itself, the default home
    if it looks like a board, and each of the default home's immediate child
    directories that looks like one -- so a session that never saw
    MAESTRO_HOME (no repo chpwd hook: CI, the desktop/web app) still protects
    the real, differently-named board(s) it finds, and so a deletion of the
    default home itself is caught even when the resolved home is a
    differently-named child of it. Bounded: one ``iterdir()`` plus a handful
    of ``stat``s per candidate, never a recursive scan."""
    roots = [home]
    try:
        default_home = Path(_DEFAULT_HOME_RAW).expanduser().resolve()
    except (OSError, RuntimeError):
        return roots
    if default_home != home and _looks_like_board(default_home):
        roots.append(default_home)
    try:
        children = list(default_home.iterdir()) if default_home.is_dir() else []
    except OSError:
        children = []
    for child in children:
        try:
            if not child.is_dir():
                continue
            resolved = child.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved in roots:
            continue
        if _looks_like_board(resolved):
            roots.append(resolved)
    return roots


# Verbs/operators that can destroy or corrupt files. The lookbehind excludes
# \w/./- so this doesn't fire inside an unrelated word ("confirm", "warmup",
# "backup.rm", a "-rm" flag) -- but deliberately does NOT exclude "/", so an
# absolute or relative binary invocation (`/bin/rm ...`, `bin/rm ...`) still
# matches: excluding "/" here originally let a path-qualified invocation
# (common to bypass a `rm` shell alias) slip through undetected.
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
# checked independently (a leading harmless clause must not hide a
# destructive one that follows it).
_CLAUSE_SPLIT_RE = re.compile(r"&&|\|\||;|\|")


def _expand_vars(command: str, home: Path) -> str:
    """Textually substitute the MAESTRO_HOME/MHOME/HOME shell variables and a
    leading tilde so path matching works even when the command references
    the home via a shell variable rather than a literal path."""
    command = re.sub(r"\$\{MAESTRO_HOME\}|\$MAESTRO_HOME\b", str(home), command)
    command = re.sub(r"\$\{MHOME\}|\$MHOME\b", str(home), command)
    home_dir = str(Path.home())
    command = re.sub(r"\$\{HOME\}|\$HOME\b", home_dir, command)
    command = re.sub(r"(?<![\w])~(?=/|\s|$)", home_dir, command)
    return command


def _strip_trailing_glob(path_str: str) -> str:
    """A shell expands a trailing wildcard to "everything in that directory"
    -- a wildcarded reference to a board root deletes events/tickets/inbox/
    config.toml just as thoroughly as a bare reference to that root does,
    even though the literal string never equals the root path. Strip a bare
    `*`/`**` down to "." (the implicit-cwd case) and a path-qualified
    trailing wildcard (`<dir>/*`, `<dir>/**`) down to `<dir>`, so both
    resolve and get checked exactly like a direct reference to that
    directory."""
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


def _is_protected(
    path_str: str, cwd: Path, roots: list[Path], *, check_ancestor: bool = True
) -> bool:
    """check_ancestor gates only the root-ancestor rule below. A caller
    checking an EXPLICIT path token typed in the command (a real argument to
    a risky verb, or a redirect target) leaves it True. A caller checking
    the AMBIENT cwd through the implicit dot-fallback (a bare verb with no
    path argument, or a bare git-clean) must pass False: cwd is always some
    real filesystem ancestor of wherever a board root happens to sit
    whenever the process just happens to be running from deeper in the tree
    (every tmp-based test home nests under the system tmp dir; any ordinary
    reconciler cwd nests under its own worktree, itself under the board) --
    honoring the ancestor rule there would false-block an unrelated ambient
    command, since it has no bearing on whether that command actually acts
    on the ancestor path, unlike a real typed reference to it."""
    path_str = _strip_trailing_glob(path_str)
    try:
        p = Path(path_str)
        p = p.resolve() if p.is_absolute() else (cwd / p).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for root in roots:
        for rel in PROTECTED_RELATIVE:
            target = root / rel if rel else root
            try:
                target = target.resolve()
            except (OSError, RuntimeError):
                continue
            if p == target:
                return True
            if not rel:
                # The root itself: an ancestor of a board root is just as
                # destructive to delete/move as the root itself is, since
                # a board root always nests under both its own parent and
                # the user's home -- `p in target.parents` catches exactly
                # that. The REVERSE (`target in p.parents`, i.e. p is
                # *inside* root) must not match here, or every relative path
                # anywhere in a reconciler's own worktree (nested under
                # root) would count -- root is trivially an ancestor of any
                # path resolved against a cwd that is itself under root.
                # That false-positived nearly every ordinary Bash call (rm
                # of a build dir, mv, a plain redirect) once this hook was
                # first wired in.
                if check_ancestor and p in target.parents:
                    return True
            else:
                # The named subtrees (events/tickets/inbox): matching
                # anything nested inside them (not just an exact-match on
                # the subtree root) is exactly the intent -- a deletion
                # reaching into one of these subtrees must be caught even
                # though it doesn't equal the subtree root itself.
                if rel != "config.toml" and target in p.parents:
                    return True
    return False


def _redirect_targets(clause: str) -> list[str]:
    """Extract destination path(s) following a truncating redirect. A quoted
    target that strips down to nothing (e.g. a bare closing quote right after
    the redirect character, which can happen when unrelated text elsewhere in
    the same clause -- an angle-bracketed email address, say -- coincidentally
    matches the redirect regex) is not a real path: letting it through would
    hand ``_is_protected`` an empty string, which ``_strip_trailing_glob``
    normalizes to the filesystem root, itself trivially an ancestor of every
    board root -- a false block on text with no redirect in it at all."""
    targets = []
    for m in _REDIRECT_RE.finditer(clause):
        rest = clause[m.end():].strip()
        if not rest:
            continue
        target = rest.split()[0].strip("'\"")
        if target:
            targets.append(target)
    return targets


def check_command(command: str, cwd: Path, home: Path) -> str | None:
    """Return a block reason if *command* is destructive against *home*, or
    against any other board root ``protected_roots(home)`` finds (the
    default home and its board-like children, plus every ancestor of every
    such root) -- else None."""
    command = _expand_vars(command, home)
    roots = protected_roots(home)
    for clause in _CLAUSE_SPLIT_RE.split(command):
        clause = clause.strip()
        if not clause:
            continue

        # `git clean` acts on the cwd's working tree by default, and can also
        # take an explicit pathspec -- check both, ignoring the "git"/"clean"
        # tokens themselves so the block message names a real path.
        if _GIT_CLEAN_RE.search(clause):
            if _is_protected(".", cwd, roots, check_ancestor=False):
                return f"{clause!r} would run inside protected MAESTRO_HOME path {cwd}"
            for p in _candidate_paths(clause):
                if p in ("git", "clean"):
                    continue
                if _is_protected(p, cwd, roots):
                    return f"{clause!r} targets protected MAESTRO_HOME path {p!r}"
            continue

        risky_verb = _RISKY_VERB_RE.search(clause)
        paths = _candidate_paths(clause, skip_leading=risky_verb.group(1)) if risky_verb else []
        paths += _redirect_targets(clause)
        for p in paths:
            if _is_protected(p, cwd, roots):
                return f"{clause!r} targets protected MAESTRO_HOME path {p!r}"
        # A bare `rm`/`mv`/`truncate` with no real path token -- e.g. `rm -rf`
        # or `rm -rf *` (a glob has nothing to resolve to) -- implicitly acts
        # on the cwd, so check that too.
        if risky_verb and not paths and _is_protected(".", cwd, roots, check_ancestor=False):
            return f"{clause!r} would act on protected MAESTRO_HOME path {cwd}"
    return None


BLOCK_MESSAGE_SUFFIX = (
    "MAESTRO_HOME's events/tickets/inbox/config.toml are the sole "
    "source of truth and have no other copy -- deleting them is "
    "unrecoverable (see CLAUDE.md). If you genuinely need to reset "
    "this home, run `maestro backup` first and get the human's "
    "explicit, in-the-moment go-ahead."
)


def block_message(reason: str) -> str:
    """The full human-facing message an adapter prints to stderr when it blocks
    *reason* -- one wording, shared by every adapter (the hook, the argv adapter),
    so ``BLOCKED by`` + this exact suffix is the one shape a test can match on."""
    return f"BLOCKED by block-home-deletion hook: {reason}\n{BLOCK_MESSAGE_SUFFIX}"
