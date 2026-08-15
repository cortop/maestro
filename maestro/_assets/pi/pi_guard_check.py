#!/usr/bin/env python3
"""T-59/PI-7: the single check entrypoint the pi guard extension shells out to.

pi ships no permission system of its own (no sandbox, no PreToolUse-style hook,
no declarative per-tool ask/allow/deny map) -- see the pi docs' own Security
page. This script IS the whole containment boundary for a pi-run reconciler,
combined from four independently-sourced checks, none hand-copied:

1. Destructive commands against MAESTRO_HOME (rm/mv/truncate/git-clean/a
   truncating redirect) -- delegates to ``destructive_command_guard.check_command``,
   imported unmodified from this script's own sibling file (same module Claude
   Code's PreToolUse hook and opencode's ``tool.execute.before`` plugin both
   call) -- never a second copy of ``PROTECTED_RELATIVE`` or the risky-verb
   regex. That module lives outside the ``maestro`` package on purpose (see its
   own docstring) and this script cannot edit it (it is harness-protected in
   this repo); reusing its PRIVATE helpers (``_RISKY_VERB_RE``,
   ``_candidate_paths``, ``_CLAUSE_SPLIT_RE``, ``_redirect_targets``,
   ``_strip_trailing_glob``, ``_expand_vars``, ``_is_protected``) below is the
   SAME reuse-not-copy convention ``maestro/runner_permissions.py`` already
   uses for the opencode permission block (extracting from the compiled regex
   object rather than retyping the verb list).
2. pi-only widening (spec Note 24): unlike Claude (3 other containment layers:
   settings.json's deny list, the PreToolUse hook, tier/phase denylist) and
   opencode (a declarative permission block), pi has ONLY this script -- so
   the narrowing that's safe for those runners (the reconciler's own worktree,
   and this guard's own install directory, stay unprotected) is NOT safe here.
   Widened to also block: `rm -rf .` / `git worktree remove` targeting the
   reconciler's own cwd, and any write/edit whose target resolves inside this
   script's own directory (disarming a later spawn by overwriting the guard).
3. The human-only maestro verb denylist -- sourced from
   ``dispatcher.AGENT_TOOL_VERBS``, baked into this directory's
   ``pi_guard_data.json`` sidecar by ``maestro.pi_guard.install`` at spawn
   time (never hand-typed here).
4. `git push --force`/`-f` -- sourced from ``maestro.pi_guard.FORCE_PUSH_GLOBS``
   (mirrors `.claude/settings.json`'s own deny rules, cross-checked by
   ``tests/test_pi_guard.py``), baked into the same sidecar.

CLI (mirrors ``guard_argv.py``'s own ``--check`` UX -- exit 0 allow, 2 block,
reason on stderr via the SAME ``block_message`` wording):

    pi_guard_check.py --check-bash <command>
    pi_guard_check.py --check-path <path>

Fails CLOSED like ``guard_argv.py`` (never the Claude-hook's fail-open-on-
malformed-input posture): a missing/corrupt ``pi_guard_data.json`` sidecar
collapses to an EMPTY allowed-verb set (every `maestro <verb>` blocked, not
silently granted) -- see ``_load_data``.
"""
from __future__ import annotations

import fnmatch
import json
import re
import sys
from pathlib import Path

import destructive_command_guard as core

_HERE = Path(__file__).resolve().parent
_DATA_PATH = _HERE / "pi_guard_data.json"

# `(?:^|[\s;&|])` -- a verb invocation starts a clause or follows a control
# operator, matching `destructive_command_guard`'s own clause-boundary idiom.
# `(?:\S*/)?maestro` -- bare or path-qualified (`.venv/bin/maestro foo`).
_MAESTRO_VERB_RE = re.compile(r"(?:^|[\s;&|])(?:\S*/)?maestro\s+([\w-]+)")
_GIT_WORKTREE_REMOVE_RE = re.compile(r"\bgit\s+worktree\s+remove\b")


def _load_data() -> dict:
    try:
        return json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"allowed_verbs": [], "force_push_globs": []}


def _verb_reason(command: str, allowed_verbs) -> str | None:
    allowed = set(allowed_verbs)
    for clause in core._CLAUSE_SPLIT_RE.split(command):
        for m in _MAESTRO_VERB_RE.finditer(clause):
            verb = m.group(1)
            if verb not in allowed:
                return (f"{clause.strip()!r} invokes maestro verb {verb!r}, which is not in "
                        "the reconciler's allowed verb set (dispatcher.AGENT_TOOL_VERBS)")
    return None


def _force_push_reason(command: str, globs) -> str | None:
    for clause in core._CLAUSE_SPLIT_RE.split(command):
        clause = clause.strip()
        if not clause:
            continue
        for glob in globs:
            if fnmatch.fnmatchcase(clause, glob):
                return (f"{clause!r} is a force-push, blocked to match .claude/settings.json's "
                        "own deny rules")
    return None


def _resolves_to(path_str: str, cwd: Path, target: Path) -> bool:
    path_str = core._strip_trailing_glob(path_str)
    try:
        p = Path(path_str)
        p = p.resolve() if p.is_absolute() else (cwd / p).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return p == target


def _worktree_widening_reason(command: str, cwd: Path, home: Path) -> str | None:
    expanded = core._expand_vars(command, home)
    cwd_resolved = cwd.resolve()
    for clause in core._CLAUSE_SPLIT_RE.split(expanded):
        clause = clause.strip()
        if not clause:
            continue
        if _GIT_WORKTREE_REMOVE_RE.search(clause):
            candidates = [p for p in core._candidate_paths(clause) if p not in ("git", "worktree", "remove")]
            if not candidates:
                candidates = ["."]
            for p in candidates:
                if _resolves_to(p, cwd, cwd_resolved):
                    return f"{clause!r} would remove the reconciler's own worktree {cwd}"
            continue
        m = core._RISKY_VERB_RE.search(clause)
        if not m:
            continue
        paths = core._candidate_paths(clause, skip_leading=m.group(1))
        paths += core._redirect_targets(clause)
        for p in paths:
            if _resolves_to(p, cwd, cwd_resolved):
                return f"{clause!r} targets the reconciler's own worktree {cwd}"
        if not paths and _resolves_to(".", cwd, cwd_resolved):
            return f"{clause!r} would act on the reconciler's own worktree {cwd}"
    return None


def check_bash(command: str, cwd: Path, home: Path, data: dict) -> str | None:
    return (core.check_command(command, cwd, home)
            or _worktree_widening_reason(command, cwd, home)
            or _verb_reason(command, data.get("allowed_verbs", []))
            or _force_push_reason(command, data.get("force_push_globs", [])))


def check_path(path_str: str, cwd: Path, home: Path) -> str | None:
    if core._is_protected(path_str, cwd, home):
        return f"write/edit target {path_str!r} is a protected MAESTRO_HOME path"
    try:
        p = Path(path_str)
        p = p.resolve() if p.is_absolute() else (cwd / p).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if p == _HERE or _HERE in p.parents:
        return (f"write/edit target {path_str!r} is inside the pi guard's own install "
                f"directory {_HERE} -- overwriting it would disarm every later spawn")
    return None


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("--check-bash", "--check-path"):
        print("usage: pi_guard_check.py --check-bash <command> | --check-path <path>", file=sys.stderr)
        return 2
    mode = argv[1]
    arg = argv[2] if len(argv) == 3 else " ".join(argv[2:])
    cwd = Path.cwd()
    home = core.resolve_home()
    if mode == "--check-bash":
        reason = check_bash(arg, cwd, home, _load_data())
    else:
        reason = check_path(arg, cwd, home)
    if reason:
        print(core.block_message(reason), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
