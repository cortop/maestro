"""Describe the destructive-command guard to a non-Claude runner's own declarative
permission config (T-34/RF-5), built from the SAME predicate source as the Claude
hook and the argv adapter -- never a second, hand-maintained copy of the protected
path list or the risky-verb set.

Why this has to live here and not beside the predicate: ``.claude/hooks/
destructive_command_guard.py`` is deliberately stdlib-only with zero dependency on
the ``maestro`` package (see that module's docstring), so nothing in ``maestro/``
may be imported *by* it -- the dependency only runs the other way, this module
importing that one. It is loaded by file path (``importlib``, not a normal package
import) because it lives outside the ``maestro`` package on purpose and is not
installed as a distributable module.

opencode's declarative model (a static per-tool ``ask|allow|deny`` map, keyed by a
glob pattern matched against the bash tool's command string) cannot express the
real predicate's shell-aware logic -- ``$MAESTRO_HOME``/``$HOME``/``~`` expansion,
clause splitting on ``&&``/``;``/``|``, trailing-wildcard stripping, or resolving a
relative path against the actual cwd (see ``check_command``'s docstring). What this
module produces is a coarser, best-effort complement for a runner that has no
hook mechanism at all -- not a claim of equivalence. It denies the exact bypass a
PATH shim structurally cannot catch (a path-qualified invocation like
``/bin/rm``, since opencode's permission engine matches the command STRING before
any PATH search happens) -- see ``guard_argv.py``'s module docstring for why a PATH
shim alone is insufficient.

opencode's own glob-matching semantics are UNVERIFIED beyond "the permission block
is accepted and echoed back verbatim by `opencode debug config`" (spiked manually,
see T-34's ``Note`` breadcrumb) -- no live run has exercised these deny patterns
specifically (the live-run spike used a plugin hook, not this declarative block).
Treat the patterns below as intentionally coarse and over-inclusive, matching this
module's own stated non-goal of precision.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_CORE_MODULE_NAME = "destructive_command_guard"
_CORE_RELATIVE_PATH = Path(".claude/hooks/destructive_command_guard.py")


def _repo_root() -> Path:
    # maestro/runner_permissions.py -> maestro/ -> repo root.
    return Path(__file__).resolve().parents[1]


def _load_core():
    """Load ``destructive_command_guard`` by file path. Raises ``FileNotFoundError``
    rather than silently falling back to a second, hand-copied protected-path list
    when the core module isn't present -- a checkout without ``.claude/hooks/`` has
    no guard to describe in the first place."""
    core_path = _repo_root() / _CORE_RELATIVE_PATH
    if not core_path.exists():
        raise FileNotFoundError(
            f"{core_path} not found -- runner_permissions can only describe the "
            "guard for a checkout that carries .claude/hooks/destructive_command_"
            "guard.py (this project's own dogfood home); it refuses to silently "
            "fall back to a second, independently-maintained copy of the protected "
            "path list."
        )
    spec = importlib.util.spec_from_file_location(_CORE_MODULE_NAME, core_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _risky_verbs(core) -> tuple[str, ...]:
    """The risky-verb set, extracted from the core's own compiled
    ``_RISKY_VERB_RE`` pattern rather than hand-copied here -- so this can never
    silently drift from what ``check_command`` actually enforces."""
    m = re.search(r"\(([\w|]+)\)", core._RISKY_VERB_RE.pattern)
    if not m:
        raise RuntimeError(
            "could not extract the risky-verb list from destructive_command_guard's "
            "_RISKY_VERB_RE -- its pattern shape changed; update the extraction here "
            "to match, don't hand-copy a verb list instead"
        )
    return tuple(m.group(1).split("|"))


def opencode_bash_permissions(home: Path) -> dict[str, str]:
    """A declarative opencode ``permission.bash`` block (glob pattern -> action)
    denying the destructive-verb/protected-path combinations the Python predicate
    blocks, for a runner (e.g. opencode) whose only enforcement surface is this
    static config -- see the module docstring for exactly what this can and can't
    guarantee. Every pattern is anchored on ``PROTECTED_RELATIVE`` and the risky
    verbs read live off ``destructive_command_guard`` (never restated by hand).
    """
    core = _load_core()
    home = Path(home).expanduser().resolve()
    verbs = _risky_verbs(core)

    perms: dict[str, str] = {}
    for rel in core.PROTECTED_RELATIVE:
        target = str(home / rel) if rel else str(home)
        for verb in verbs:
            # Bare, `/bin/`-qualified, and `bin/`-qualified forms -- the
            # path-qualified ones are exactly the PATH-shim's blind spot
            # (see module docstring), caught here instead.
            perms[f"*{verb} *{target}*"] = "deny"
            perms[f"*/{verb} *{target}*"] = "deny"
        perms[f"*git clean*{target}*"] = "deny"
        perms[f"*>*{target}*"] = "deny"
    # A bare destructive verb with no path argument acts on the runner's cwd --
    # check_command resolves that against the real cwd; a static glob can't, so
    # this falls back to "ask" (never a silent allow) rather than a blanket deny
    # that would also refuse every legitimate rm/mv/truncate call.
    for verb in verbs:
        perms.setdefault(verb, "ask")
    perms.setdefault("git clean", "ask")
    return perms


# T-64: the three MAESTRO_HOME subtrees every reconcile skill's Read tool calls
# actually touch (spec.md, the folded context file, snapshots/cursors) -- see
# opencode_permission_block's docstring for why this can't be a home-wide or
# empty-string catch-all.
_EXTERNAL_DIRECTORY_SUBTREES = ("tickets", "inbox", "derived")


def opencode_permission_block(home: Path) -> dict:
    """T-64: the full declarative ``permission`` block ``skills_install`` writes
    into opencode.jsonc -- the write path both this function and
    ``opencode_bash_permissions`` (T-34/RF-5) were left waiting on (see that
    function's own docstring / ``sessions.py:341-347``).

    Two keys:

    ``bash`` -- ``opencode_bash_permissions`` (the destructive-command guard,
    unchanged) plus an explicit ALLOW for the maestro-verb surface a reconciler
    actually invokes (``dispatcher.AGENT_TOOL_VERBS`` -- the SAME definition
    ``cli._reconciler_tool_grants`` renders into Claude's ``--allowedTools``, so
    this can never drift into a second, hand-typed verb list). Belt-and-suspenders:
    opencode's own bash default is already allow (nothing here relies on that
    staying true), and these entries can never collide with the guard's deny
    patterns (those are all ``rm``/``mv``/``truncate``/``git clean``/redirect
    patterns, never ``maestro *``).

    ``external_directory`` -- T-64's actual fix for the failure the spec's Notes
    describe: every reconcile skill Reads ``<MAESTRO_HOME>/tickets/<KEY>/spec.md``
    and ``.../derived/context/<KEY>.md`` via the Read tool, and MAESTRO_HOME is
    never inside opencode's own ``--dir`` (the ticket's worktree) -- opencode
    treats that as an ``external_directory`` permission request, defaults it to
    ``ask``, and a headless (no-TTY) ``opencode run`` auto-rejects every ``ask``.
    Verified empirically (T-64 spec Notes' own methodology): a real ``opencode
    run`` denies with "The user rejected permission..." without this block and
    succeeds with it, against the SAME three-subtree glob shape below (spiked
    against a real ``qwen3-coder:30b`` tool call, not just ``opencode debug
    config`` echo). Scoped to exactly the three subtrees the skills read from
    (``tickets``/``inbox``/``derived`` -- covers spec.md, inbox logs, and both
    snapshots and the folded context file under one glob) -- never a home-wide
    or bare ``"*"`` entry, so this can't be mistaken for the blanket-approve
    posture the module docstring forbids.
    """
    from . import dispatcher  # lazy: avoids a load-time cycle (matches repos.resolve's own note)

    bash = dict(opencode_bash_permissions(home))
    for verb in dispatcher.AGENT_TOOL_VERBS:
        bash[f"maestro {verb} *"] = "allow"
        bash[f"maestro {verb}"] = "allow"

    home = Path(home).expanduser().resolve()
    external_directory = {
        f"{home / sub}/**": "allow" for sub in _EXTERNAL_DIRECTORY_SUBTREES
    }
    return {"bash": bash, "external_directory": external_directory}
