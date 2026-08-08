"""GA-15: ``maestro install-commands`` — automated distribution of the six
per-phase ``.claude/commands/maestro-reconcile-*.md`` files, replacing the
"vendor them by hand" step DOGFOOD.md used to document (MR-6 deferred this;
never filed until now). Two targets: ``--repo <name>`` copies the files into a
configured [repos.<name>] checkout (they need to be real, committed files —
that repo may be owned by someone else and reviews them like any other
change); ``--user`` symlinks them into a user commands directory that resolves
from any cwd, for a repo the board does not own.

Both targets are idempotent: a second run leaves identical on-disk state, never
duplicates or nests directories, and never touches a file this verb did not
install itself.

The payload is resolved via ``importlib.resources`` against the INSTALLED
``maestro`` package (never a repo-root-relative path — that only works under
an editable install and breaks on a real one). ``maestro/_skill_commands/``
holds one symlink per phase file pointing at the canonical
``.claude/commands/maestro-reconcile-<phase>.md`` copy, so there is exactly one
byte-editable source of truth: hatchling dereferences those symlinks into real
file content when it builds a wheel, and under an editable install the
symlinks resolve on disk exactly as they do in this checkout — either way
``payload_dir()`` finds real, readable files with no build step.
"""
from __future__ import annotations

import importlib.resources
import os
from pathlib import Path

from . import store
from .config import Config

# The six phase files T-22 split the reconcile skill into (see
# tests/test_reconcile_skill.py:36). Excludes maestro-task.md — a human
# ticket-creation command no reconciler ever invokes, so a bound repo doesn't
# need it under .claude/commands/.
PHASE_FILES = ("triaging", "awaiting-human", "ready", "researching", "implementing", "passive")
PAYLOAD_NAMES = tuple(f"maestro-reconcile-{phase}.md" for phase in PHASE_FILES)


def payload_dir() -> Path:
    """The installed package's copy of the six command files."""
    return Path(importlib.resources.files("maestro")) / "_skill_commands"


def user_commands_dir(cfg: Config | None = None) -> Path:
    """Where ``--user`` installs land and where the doctor check falls back to.

    Override precedence: ``MAESTRO_USER_COMMANDS_DIR`` env var >
    ``cfg.user_commands_dir`` > ``~/.claude/commands``. Both overrides exist so
    tests (and doctor's machine-independence requirement) never have to depend
    on a developer's real ``~/.claude``.
    """
    env = os.environ.get("MAESTRO_USER_COMMANDS_DIR")
    if env:
        return Path(env).expanduser()
    if cfg is not None and cfg.user_commands_dir:
        return Path(cfg.user_commands_dir).expanduser()
    return Path.home() / ".claude" / "commands"


def _configured_repo_names(cfg: Config) -> list[str]:
    names = sorted(cfg.repos.keys())
    if cfg.repo_path:
        names = ["default"] + names
    return names


def resolve_repo_target(cfg: Config, name: str) -> Path:
    """Filesystem path for ``--repo <name>``: a configured ``[repos.<name>]``
    table, or the sentinel ``"default"`` for the legacy single-repo
    ``repo_path`` (matches ``repos.implicit_default``'s synthesized name).
    Raises, naming the configured options, on anything else — never guesses.
    """
    table = cfg.repos.get(name)
    if table and table.get("path"):
        return Path(table["path"])
    if name == "default" and cfg.repo_path:
        return Path(cfg.repo_path)
    configured = _configured_repo_names(cfg)
    raise store.MaestroError(
        f"unknown repo '{name}'; configured repos: {', '.join(configured) if configured else 'none'}")


def _write_if_changed(target: Path, content: str) -> bool:
    """Atomically write *content* to *target* unless it's already identical.
    Returns whether a write happened (for the verb's reported summary)."""
    if target.exists() and not target.is_symlink() and target.read_text(encoding="utf-8") == content:
        return False
    store.atomic_write(target, content)
    return True


def install_repo(cfg: Config, name: str) -> dict:
    """Copy the six payload files into ``<repo>/.claude/commands/`` as real,
    byte-identical files — the target repo may not be this one, and needs to
    be able to commit + review them like any other change."""
    target_dir = resolve_repo_target(cfg, name) / ".claude" / "commands"
    src_dir = payload_dir()
    written = []
    for filename in PAYLOAD_NAMES:
        content = (src_dir / filename).read_text(encoding="utf-8")
        dest = target_dir / filename
        if _write_if_changed(dest, content):
            written.append(filename)
    return {"target": str(target_dir), "installed": list(PAYLOAD_NAMES), "written": written}


def install_user(cfg: Config) -> dict:
    """Symlink the six payload files into the user commands directory.

    Idempotent: a symlink already pointing at the payload is left alone; a
    symlink pointing anywhere else (a stale install after a package upgrade
    moved ``payload_dir()``) is repointed; a real file at that path that this
    verb didn't create is never touched — refuse instead of clobbering
    whatever a human put there.
    """
    target_dir = user_commands_dir(cfg)
    src_dir = payload_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    conflicts = []
    for filename in PAYLOAD_NAMES:
        dest = target_dir / filename
        if dest.exists() and not dest.is_symlink():
            conflicts.append(str(dest))
    if conflicts:
        raise store.MaestroError(
            "refusing to overwrite file(s) install-commands did not create: "
            + ", ".join(conflicts))

    written = []
    for filename in PAYLOAD_NAMES:
        src = src_dir / filename
        dest = target_dir / filename
        if dest.is_symlink() and Path(os.readlink(dest)) == src:
            continue  # already correct -- leave it alone
        if dest.is_symlink() or dest.exists():
            dest.unlink()  # stale symlink (or something we already validated isn't a real file)
        dest.symlink_to(src)
        written.append(filename)
    return {"target": str(target_dir), "installed": list(PAYLOAD_NAMES), "written": written}
