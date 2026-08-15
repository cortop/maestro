"""Installs the pi guard extension (T-59/PI-7) and describes the argv every pi
invocation maestro constructs must carry.

pi is auto-approve by design (see the pi docs' own Security page): no sandbox,
no permission prompts, a headless run with stdin closed executes bash with
zero gating. Unlike Claude Code (where maestro must NOT opt in to auto-approve)
the rule inverts for pi: the dangerous state is the default, and this module
is the opt-out. ``gates.BYPASS_RESISTANT_IMPLEMENTERS`` refuses to spawn `pi`
until every check this module wires in is proven live (see that module's own
comment) -- adding `"pi"` there is deliberately the LAST commit of this
ticket, not the first.

The actual predicate logic lives in ``maestro/_assets/pi/pi_guard_check.py``
(imports ``destructive_command_guard`` unmodified -- see that script's module
docstring for the full source chain); this module's job is purely mechanical:
materialize those packaged assets into a real, per-home install path
(``install``), and describe the argv flags that reference them
(``spawn_argv``).
"""
from __future__ import annotations

import importlib.resources
import json
from pathlib import Path

from . import store

# T-59: mirrors `.claude/settings.json`'s own `permissions.deny` force-push
# rules (`Bash(git push* --force*)`, `Bash(git push* -f*)`) as bare fnmatch
# globs (no Claude `Bash(...)` wrapper) so `pi_guard_check.py`'s stdlib
# `fnmatch` can apply them directly to a bash clause. This is the ONE
# definition -- `tests/test_pi_guard.py` cross-checks it against the literal
# settings.json deny rules so the two can never silently drift apart (a
# runtime read of `.claude/settings.json` itself would tie the packaged wheel
# to a repo checkout that doesn't exist post-install -- see this module's own
# docstring on why the predicate script can't do that either).
FORCE_PUSH_GLOBS = ("git push* --force*", "git push* -f*")

# T-59 Note 22: pi's own `--tools`/`-t` allowlist. A REAL execution boundary
# (unlike `--exclude-tools`, verified: `write` stays registered and succeeds
# under it) -- but NOT the containment boundary this ticket builds: it rejects
# a tool call only AFTER pi has already emitted `tool_execution_start` with
# arguments, and an unknown/misspelled name in it is silently ignored (never
# an error). `tool_call` interception (`pi_guard_check.py`, wired via
# `spawn_argv`'s `--extension`) is the actual boundary; this constant is
# documentation of intent -- a narrower default surface, nothing more -- so it
# is a frozen module-level literal, never read from config (a board could
# otherwise widen it by editing `config.toml`, silently defeating the whole
# point of freezing it). `grep`/`find`/`ls` are deliberately absent: pi lists
# them as read-only and OFF BY DEFAULT, so naming them here would ENABLE them,
# not narrow anything.
PI_GUARD_TOOLS = ("bash", "read", "write", "edit")

_ASSET_SUBDIR = "pi"
_STATIC_ASSETS = ("destructive_command_guard.py", "pi_guard_check.py", "pi_guard_extension.ts")
_EXTENSION_FILENAME = "pi_guard_extension.ts"
_DATA_FILENAME = "pi_guard_data.json"


def _package_asset_dir() -> Path:
    return Path(importlib.resources.files("maestro")) / "_assets" / _ASSET_SUBDIR


def _extensions_dir(pi_agent_dir: Path) -> Path:
    return Path(pi_agent_dir) / "extensions"


def extension_path(pi_agent_dir: Path) -> Path:
    """Where `install` materializes the guard extension for *pi_agent_dir* --
    the same path `spawn_argv`'s `--extension` flag names. T-61:
    `health.check_reconciler_permissions`'s own read-only existence probe for
    a pi binding (never calls `install` itself -- a doctor check must never
    write) uses this to name the exact path it checked."""
    return _extensions_dir(pi_agent_dir) / _EXTENSION_FILENAME


def _write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    store.atomic_write(path, content)
    return True


def install(pi_agent_dir: Path) -> Path:
    """Materialize the pi guard extension + its checker + the predicate it
    reuses under ``<pi_agent_dir>/extensions/`` -- write-if-changed (mirrors
    ``store.generate_pi_models_json``), so calling this on every spawn is a
    no-op once the content already matches. Returns the ``.ts`` extension's
    path -- the ``--extension`` argv value (see ``spawn_argv``).

    The three static files are packaged under ``maestro/_assets/pi/``
    (``destructive_command_guard.py`` is a symlink into ``.claude/hooks/``,
    dereferenced into real content by hatchling at wheel build -- the same
    precedent ``maestro/_skill_commands/`` already establishes, see
    ``pyproject.toml``), so this resolves identically from an editable
    checkout and a real ``pip install``. ``pi_guard_data.json`` is the one
    GENERATED file: baked fresh from ``dispatcher.AGENT_TOOL_VERBS`` (lazy
    import -- avoids the load-time cycle ``sessions -> pi_guard -> dispatcher
    -> sessions``, matching ``runner_permissions.opencode_permission_block``'s
    own precedent) and ``FORCE_PUSH_GLOBS`` on every call, exactly like
    ``generate_pi_models_json`` bakes ``[runner.pi]`` into ``models.json``.
    """
    from . import dispatcher  # lazy: avoids a load-time import cycle (see docstring)

    dest_dir = _extensions_dir(pi_agent_dir)
    src_dir = _package_asset_dir()
    for name in _STATIC_ASSETS:
        content = (src_dir / name).read_text(encoding="utf-8")
        _write_if_changed(dest_dir / name, content)

    data = json.dumps({
        "allowed_verbs": sorted(dispatcher.AGENT_TOOL_VERBS),
        "force_push_globs": list(FORCE_PUSH_GLOBS),
    }, indent=2, sort_keys=True) + "\n"
    _write_if_changed(dest_dir / _DATA_FILENAME, data)

    return dest_dir / _EXTENSION_FILENAME


def spawn_argv(pi_agent_dir: Path) -> list[str]:
    """The argv fragment EVERY pi invocation maestro constructs must carry:
    the guard extension explicitly loaded, ALL other extension discovery
    disabled (so a hostile or misconfigured project can't shadow it with its
    own ``.pi/extensions/``), the frozen tool allowlist (see
    ``PI_GUARD_TOOLS``'s docstring), and project-local file trust declined for
    the run. ``--approve``/``-a`` must never appear in an argv maestro builds
    (T-59 AC9 -- see ``tests/test_runner_permissions.py``'s forbidden-literal
    sweep); ``--no-approve`` is the explicit opt-out, belt-and-suspenders next
    to non-interactive mode's own default (pi's own docs: a non-interactive
    run already ignores project-local resources unless
    ``defaultProjectTrust: "always"`` is configured).

    Calls ``install(pi_agent_dir)`` first, so the ``--extension`` path this
    returns always exists before an argv referencing it is ever used.
    """
    extension_path = install(pi_agent_dir)
    return [
        "--extension", str(extension_path),
        "--no-extensions",
        "--tools", ",".join(PI_GUARD_TOOLS),
        "--no-approve",
    ]
