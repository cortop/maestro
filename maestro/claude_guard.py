"""Installs and verifies the maestro-owned Claude settings file that wires the PreToolUse destructive-command guard into every claude spawn."""
from __future__ import annotations

import importlib.resources
import json
import shlex
from pathlib import Path

from . import store

_ASSET_SUBDIR = "claude"
_HOOK_SCRIPT = "block-home-deletion.py"
_STATIC_ASSETS = (_HOOK_SCRIPT, "destructive_command_guard.py")
_SETTINGS_FILENAME = "settings.json"


# Limit: the hook is a textual check over the Bash command string (see
# destructive_command_guard's docstring). It cannot see what an allowed
# interpreter does internally, e.g. the granted `Bash(python3:*)` running a
# script that deletes files, nor command substitution or piped xargs targets.


class ClaudeGuardError(RuntimeError):
    """The guard settings file or its hook script is missing or unusable."""


def _package_asset_dir() -> Path:
    return Path(importlib.resources.files("maestro")) / "_assets" / _ASSET_SUBDIR


def settings_path(home: Path) -> Path:
    """The ``--settings`` file every claude spawn for *home* passes."""
    return store.claude_agent_dir(home) / _SETTINGS_FILENAME


def hook_script_path(home: Path) -> Path:
    return store.claude_agent_dir(home) / "hooks" / _HOOK_SCRIPT


def _settings_content(home: Path) -> str:
    # The hook runs from the installed copy next to its sibling predicate module,
    # so it resolves identically from any cwd (no $CLAUDE_PROJECT_DIR).
    command = f"python3 {shlex.quote(str(hook_script_path(home)))}"
    data = {"hooks": {"PreToolUse": [{
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": command}],
    }]}}
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _write_if_changed(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    store.atomic_write(path, content)


def install(home: Path) -> Path:
    """Materialize the hook scripts and settings file under the home's claude
    agent dir (write-if-changed, so safe on every spawn); returns the settings path."""
    src_dir = _package_asset_dir()
    for name in _STATIC_ASSETS:
        _write_if_changed(hook_script_path(home).parent / name,
                          (src_dir / name).read_text(encoding="utf-8"))
    _write_if_changed(settings_path(home), _settings_content(home))
    return settings_path(home)


def verify(home: Path) -> Path:
    """The settings path, after proving the file wires a PreToolUse Bash hook
    whose script exists; raises ``ClaudeGuardError`` otherwise."""
    path = settings_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ClaudeGuardError(f"claude guard settings unreadable at {path}: {e}") from e
    commands = []
    entries = (data.get("hooks") or {}).get("PreToolUse") if isinstance(data, dict) else None
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("matcher") == "Bash":
            commands += [h.get("command", "") for h in entry.get("hooks") or []
                         if isinstance(h, dict)]
    script = hook_script_path(home)
    if not any(str(script) in c for c in commands):
        raise ClaudeGuardError(f"claude guard settings at {path} do not wire the PreToolUse hook")
    for name in _STATIC_ASSETS:
        if not (script.parent / name).is_file():
            raise ClaudeGuardError(f"claude guard hook file missing: {script.parent / name}")
    return path


def require(home: Path) -> Path:
    """Install, then verify; the one call a claude spawn makes. Raises
    ``ClaudeGuardError`` rather than ever letting claude start unguarded."""
    try:
        install(home)
    except OSError as e:
        raise ClaudeGuardError(f"claude guard install failed: {e}") from e
    return verify(home)
