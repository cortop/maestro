#!/usr/bin/env python3
"""Argv-transport adapter around ``destructive_command_guard`` for non-Claude runners.

The Claude Code hook (``block-home-deletion.py``) reads a PreToolUse JSON payload on
stdin -- a transport only Claude Code speaks. A runner without that hook mechanism
(e.g. a plain shell wrapper, or a runner whose only integration point is "intercept
this argv/command line") needs the same predicate reachable a different way. This
script is that adapter, and it has two independent invocation shapes:

1. **PATH shim.** Placed first on ``PATH`` under the name ``rm``, ``mv``, or
   ``truncate`` (a copy or symlink -- ``sys.argv[0]``'s basename picks the verb), it
   reconstructs the command line from its own argv, checks it, and -- if allowed --
   ``exec``s the *real* binary (found by searching the rest of ``PATH``, skipping its
   own directory) so the genuine coreutils command actually runs with identical argv,
   stdio, and exit code. This is the mechanism named in CLAUDE.md/the ticket Notes as
   "strictly weaker than the hook": it cannot intercept a path-qualified invocation
   like ``/bin/rm`` (the whole point of a PATH shim is that PATH search finds it
   first, and an absolute path bypasses PATH search entirely) -- do not present it as
   equivalent to the hook.
2. **Generic adapter.** Invoked directly (any other argv[0]) as
   ``guard_argv.py [--check] <command>``, where ``<command>`` is the *whole* shell
   command line as one argument (not pre-tokenized) -- this is what a runner whose
   interception point is "the full command string", not a specific binary name, calls
   (e.g. an opencode plugin's ``tool.execute.before`` hook, which sees the bash tool's
   ``command`` argument, not argv). ``--check`` reports allow/deny only (exit 0 or 2,
   nothing executed) -- the mode a caller uses when it wants to make its own decision
   about whether/how to run the command afterward. Without ``--check``, an allowed
   command is executed via ``/bin/sh -c`` (the same interpreter a shell-driven runner
   would have used) so the adapter can also stand in as a full wrapper.

Fails **closed**: unlike the Claude hook (which fails open on unparseable stdin --
malformed JSON must never break an unrelated tool call), unparseable/missing argv
here is a bug in a wrapper we control, not untrusted input -- see the ticket Notes.
Both invocation shapes print the exact same ``block_message`` on a block, exit 2, and
run nothing.

stdlib-only, same zero-``maestro``-package-dependency constraint as
``destructive_command_guard`` (its sibling import, resolved via same-directory
``sys.path[0]`` -- no path setup needed, see that module's docstring).
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from destructive_command_guard import block_message, check_command, resolve_home

# The verbs this adapter can stand in for on PATH. Kept in sync with the risky-verb
# set destructive_command_guard.py actually checks for -- shimming a verb this guard
# doesn't recognize would be pure overhead with no protective value.
_SHIMMABLE_VERBS = {"rm", "mv", "truncate"}


def _shim_dir(argv0: str) -> Path:
    """The directory this script was invoked from, WITHOUT following a symlink --
    when placed on PATH as a symlink named e.g. ``rm``, the shell passes that
    symlink's own path (not its resolved target) as argv[0]. Resolving it would
    collapse back to this file's real location (.claude/hooks/), which is never
    the PATH entry doing the shimming -- excluding the wrong directory from the
    "find the real binary" search below would let this shim find itself again
    and recurse forever."""
    return Path(os.path.abspath(argv0)).parent


def _find_real_binary(verb: str, exclude_dir: Path) -> str | None:
    """First *verb* on PATH, skipping *exclude_dir* -- the shim's own PATH entry."""
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        entry_dir = Path(os.path.abspath(entry))
        if entry_dir == exclude_dir:
            continue
        candidate = entry_dir / verb
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def main(argv: list[str]) -> int:
    prog = Path(argv[0]).name if argv else ""
    rest = argv[1:]

    if prog in _SHIMMABLE_VERBS:
        verb: str | None = prog
        check_only = False
        command = f"{prog} {shlex.join(rest)}" if rest else prog
    else:
        check_only = bool(rest) and rest[0] == "--check"
        if check_only:
            rest = rest[1:]
        if not rest:
            print("usage: guard_argv.py [--check] <command>", file=sys.stderr)
            return 2
        verb = None
        # The whole command line is ONE argument (see module docstring) -- a caller
        # that already has it tokenized joins it back with shlex before calling this.
        command = rest[0] if len(rest) == 1 else shlex.join(rest)

    cwd = Path.cwd()
    home = resolve_home()
    reason = check_command(command, cwd, home)
    if reason:
        print(block_message(reason), file=sys.stderr)
        return 2
    if check_only:
        return 0

    if verb is not None:
        real = _find_real_binary(verb, _shim_dir(argv[0]))
        if real is None:
            print(f"guard_argv: no real {verb!r} found on PATH beyond this shim", file=sys.stderr)
            return 127
        os.execv(real, [real] + rest)
    else:
        os.execv("/bin/sh", ["/bin/sh", "-c", command])
    return 0  # unreachable -- os.execv only returns on failure (raises OSError)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
