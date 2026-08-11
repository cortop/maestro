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

T-34/RF-5: the destructive-command predicate itself now lives in
``destructive_command_guard.py``, this file's sibling, so it can also be
driven by a plain-argv adapter for non-Claude runners (``guard_argv.py``)
without a second copy of the protected-path list or heuristics. This file
is now just the Claude Code PreToolUse JSON-on-stdin adapter over that core.

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
import sys
from pathlib import Path

from destructive_command_guard import block_message, check_command, resolve_home


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
        print(block_message(reason), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
