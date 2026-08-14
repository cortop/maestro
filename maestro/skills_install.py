"""GA-15: ``maestro install-commands`` — automated distribution of the
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

OC-1: both targets ALSO install an opencode copy of the same payload, from the
SAME source — opencode resolves its own custom commands from a directory it
defines (``.opencode/command/`` in a repo checkout, ``~/.config/opencode/
command/`` user-scope), not Claude Code's. The only difference between the two
installed copies is frontmatter (``_opencode_frontmatter`` strips the two
Claude-only lines, ``allowed-tools:``/``argument-hint:``, that have no opencode
counterpart) — the body, including the ``$1`` substitution, is byte-identical.
Never a third hand-maintained source file: both copies are derived from
``payload_dir()`` at install time. Unlike the Claude ``--user`` symlink (which
can tell "ours" from "a human's file" by target equality), the opencode copy is
always a real, transformed file, so it is kept in sync unconditionally
(``_write_if_changed``, no refuse-to-clobber) for both targets — the same
posture ``install_repo``'s Claude side already has.

T-64: both targets ALSO install the two other pieces of opencode declarative
config ``sessions.py:341-347`` left waiting on a write path — an ``--agent``
stub per phase (``.opencode/agent/<phase>.md`` / ``~/.config/opencode/agent/``)
and a single ``opencode.jsonc`` (``permission`` from
``runner_permissions.opencode_permission_block`` + a ``provider`` block for
the local ``ollama`` model registration, ``_opencode_provider_block``). Same
posture as the opencode command copy: always real, transformed files, kept in
sync unconditionally, never hand-maintained (the spec Notes call this out by
name for ``opencode.jsonc`` specifically — a human hand-wrote one on the real
dogfood board before this ticket existed; this generator now owns that file).
"""
from __future__ import annotations

import importlib.resources
import json
import os
from pathlib import Path

from . import runner_permissions, store
from .config import Config
from .providers import ollama as ollama_mod
from .sessions import OPENCODE_MODEL_PROVIDER

# The phase files T-22 split the reconcile skill into, plus RF-6's `qa` (see
# tests/test_reconcile_skill.py:36). Excludes maestro-task.md — a human
# ticket-creation command no reconciler ever invokes, so a bound repo doesn't
# need it under .claude/commands/.
PHASE_FILES = ("triaging", "awaiting-human", "ready", "researching", "implementing", "qa", "passive")
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


def opencode_user_commands_dir(cfg: Config | None = None) -> Path:
    """OC-1: the opencode counterpart of ``user_commands_dir`` above -- where
    ``--user`` installs opencode's copy and where the doctor check falls back
    to for a non-``claude`` runner.

    Override precedence: ``MAESTRO_OPENCODE_COMMANDS_DIR`` env var >
    ``cfg.opencode_user_commands_dir`` > ``~/.config/opencode/command``. Same
    injectability rationale as ``user_commands_dir`` -- no test (or doctor's
    machine-independence requirement) depends on a developer's real
    ``~/.config/opencode``.
    """
    env = os.environ.get("MAESTRO_OPENCODE_COMMANDS_DIR")
    if env:
        return Path(env).expanduser()
    if cfg is not None and cfg.opencode_user_commands_dir:
        return Path(cfg.opencode_user_commands_dir).expanduser()
    return Path.home() / ".config" / "opencode" / "command"


def opencode_user_agent_dir(cfg: Config | None = None) -> Path:
    """T-64: user-scope counterpart of ``opencode_repo_agent_dir`` -- sibling of
    ``opencode_user_commands_dir`` under the same ``~/.config/opencode/``
    (verified against a real installed opencode's own on-disk layout: `command/`
    and `opencode.jsonc` are already siblings there), so every override that
    redirects ``opencode_user_commands_dir`` (env var, ``cfg``) redirects this
    too, with no separate knob to keep in sync."""
    return opencode_user_commands_dir(cfg).parent / "agent"


def opencode_user_config_path(cfg: Config | None = None) -> Path:
    """T-64: user-scope counterpart of ``opencode_repo_config_path`` -- see that
    function's docstring. This is the exact path (``~/.config/opencode/
    opencode.jsonc``) a human had been hand-maintaining on the real dogfood
    board before this ticket (spec Notes) -- this generator now owns it."""
    return opencode_user_commands_dir(cfg).parent / "opencode.jsonc"


def opencode_repo_commands_dir(repo_path: Path) -> Path:
    """OC-1: where an opencode copy of the payload lands inside a repo checkout
    -- opencode's own project-scope command directory, mirroring
    ``<repo>/.claude/commands/`` one-for-one."""
    return Path(repo_path) / ".opencode" / "command"


def opencode_repo_agent_dir(repo_path: Path) -> Path:
    """T-64: opencode's own project-scope agent directory -- sibling of
    ``opencode_repo_commands_dir`` under the same ``.opencode/``, derived from
    it rather than a second ``Path(repo_path) / ".opencode" / ...`` literal."""
    return opencode_repo_commands_dir(repo_path).parent / "agent"


def opencode_repo_config_path(repo_path: Path) -> Path:
    """T-64: where the generated ``opencode.jsonc`` (permission + provider
    blocks) lands inside a repo checkout -- verified against a real installed
    opencode (``opencode debug config`` picks up ``.opencode/opencode.jsonc``
    project-scope overrides, spiked manually for this ticket)."""
    return opencode_repo_commands_dir(repo_path).parent / "opencode.jsonc"


def _opencode_frontmatter(content: str) -> str:
    """OC-1's one documented frontmatter transform (AC1): from a source file's
    Claude Code frontmatter block, drop the two lines that have no opencode
    counterpart -- ``allowed-tools:`` (Claude Code's per-command tool
    allowlist; opencode enforces permissions through its own, board-wide
    declarative ``permission.bash`` config instead, see
    ``runner_permissions.opencode_bash_permissions``, not a per-command list)
    and ``argument-hint:`` (a Claude Code slash-command UI hint opencode has no
    equivalent surface for). Every other frontmatter line (``description:``)
    and the ENTIRE body — including the ``$1`` substitution — passes through
    byte-identical; tests assert exactly that.
    """
    parts = content.split("---\n", 2)
    if len(parts) != 3:
        return content  # no frontmatter block -- nothing to transform
    _, front, body = parts
    kept = [line for line in front.splitlines()
            if not line.startswith(("allowed-tools:", "argument-hint:"))]
    return "---\n" + "\n".join(kept) + "\n---\n" + body


def _frontmatter_description(content: str) -> str:
    """The bare ``description:`` value from a payload file's own frontmatter --
    never a second hand-typed description, so an agent stub's one-liner (below)
    can't drift from the command file it's paired with."""
    parts = content.split("---\n", 2)
    if len(parts) != 3:
        return ""
    for line in parts[1].splitlines():
        if line.startswith("description:"):
            return line[len("description:"):].strip()
    return ""


def _opencode_agent_stub(description: str) -> str:
    """T-64: the ``--agent <name>`` file ``OpencodeCliSessions.spawn``
    (``sessions.py:396``) has been passing at a path nothing ever wrote --
    every spawn logged ``agent "..." not found. Falling back to default agent``
    (spec Notes). Verified empirically (spiked against a real opencode run): a
    bare ``mode: primary`` stub with no body resolves the flag cleanly -- the
    actual prompt/body comes from the paired ``--command <name>`` (the payload
    file itself), so this file's only job is to make the name resolve.
    ``primary`` (not ``subagent``) matches ``sessions.py``'s own docstring: a
    reconciler is a TOP-LEVEL session, never a subagent."""
    return f"---\ndescription: {description}\nmode: primary\n---\n"


def _opencode_provider_block(cfg: Config) -> dict:
    """OC-6: opencode 1.18.x ships only ``ollama-cloud`` out of the box (spec
    Notes) -- a bare ``--model ollama/<tag>`` (``sessions.OPENCODE_MODEL_PROVIDER``,
    composed only in ``sessions.py``) fails with ``ProviderModelNotFoundError``
    unless a local ``ollama`` provider is registered declaratively, verified
    against a real ``opencode run`` for this ticket. Model tags come from the
    pinned, explicit ``[runner.opencode].models`` (same "pinned, generated from
    [runner.<name>] config" posture T-56 specifies for ``pi`` -- a human decides
    what to register, this never scrapes ticket specs for whatever tag happens
    to be in use), falling back to the board-wide ``runner_model`` default when
    unset. Returns ``{}`` (no ``provider`` key at all) when neither is
    configured -- nothing to register yet.
    """
    table = (cfg.provider_config.get("runner") or {}).get("opencode") or {}
    tags = table.get("models")
    if not isinstance(tags, list) or not tags:
        tags = [cfg.runner_model] if cfg.runner_model else []
    tags = [t for t in dict.fromkeys(tags) if t]
    if not tags:
        return {}
    base_url = ollama_mod.base_url(table.get("host")) + "/v1"
    return {
        OPENCODE_MODEL_PROVIDER: {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": base_url},
            "models": {tag: {"tool_call": True} for tag in tags},
        }
    }


def _opencode_declarative_config(cfg: Config) -> dict:
    """T-64: the whole generated ``opencode.jsonc`` payload -- ``permission``
    (``runner_permissions.opencode_permission_block``) always present, the local
    ``ollama`` ``provider`` block only when there's a model tag to register
    (``_opencode_provider_block``). One source both install targets serialize
    (``json.dumps(..., indent=2)`` -- pure JSON despite the ``.jsonc`` name, so
    reading it back (``read_opencode_config``, used by the doctor check) never
    needs a comment-stripping parser: this generator's own output has none, and
    unlike the Claude ``--user`` symlink this file is never left for a human to
    hand-edit -- see the module docstring)."""
    config: dict = {
        "$schema": "https://opencode.ai/config.json",
        "permission": runner_permissions.opencode_permission_block(cfg.home),
    }
    provider = _opencode_provider_block(cfg)
    if provider:
        config["provider"] = provider
    return config


def _dump_opencode_config(cfg: Config) -> str:
    return json.dumps(_opencode_declarative_config(cfg), indent=2) + "\n"


def read_opencode_config(path: Path) -> dict | None:
    """Parse a generated ``opencode.jsonc`` back into a dict -- ``None`` on a
    missing file or any parse error, never raising (health checks must never
    block a spawn on a malformed/foreign file at this path). Plain
    ``json.loads`` is sufficient: this module's own generator never emits
    comments (see ``_opencode_declarative_config``'s docstring) and always
    overwrites unconditionally, so a file at this path was either written by
    this generator or doesn't exist yet -- there is no third, hand-edited case
    to tolerate."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


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
    be able to commit + review them like any other change.

    OC-1: also copies an opencode-frontmatter (``_opencode_frontmatter``)
    transform of the SAME payload into ``<repo>/.opencode/command/`` — same
    filenames, body byte-identical, one source.

    T-64: also writes a ``--agent`` stub per phase into ``<repo>/.opencode/
    agent/`` (same filenames again — ``<name>`` is reused for both ``--command``
    and ``--agent``, per ``sessions.py``'s own docstring) and one
    ``<repo>/.opencode/opencode.jsonc`` (permission + provider blocks)."""
    repo_root = resolve_repo_target(cfg, name)
    target_dir = repo_root / ".claude" / "commands"
    opencode_dir = opencode_repo_commands_dir(repo_root)
    opencode_agent_dir = opencode_repo_agent_dir(repo_root)
    src_dir = payload_dir()
    written = []
    opencode_written = []
    opencode_agent_written = []
    for filename in PAYLOAD_NAMES:
        content = (src_dir / filename).read_text(encoding="utf-8")
        dest = target_dir / filename
        if _write_if_changed(dest, content):
            written.append(filename)
        opencode_dest = opencode_dir / filename
        if _write_if_changed(opencode_dest, _opencode_frontmatter(content)):
            opencode_written.append(filename)
        agent_dest = opencode_agent_dir / filename
        if _write_if_changed(agent_dest, _opencode_agent_stub(_frontmatter_description(content))):
            opencode_agent_written.append(filename)
    config_dest = opencode_repo_config_path(repo_root)
    config_written = _write_if_changed(config_dest, _dump_opencode_config(cfg))
    return {"target": str(target_dir), "opencode_target": str(opencode_dir),
            "opencode_agent_target": str(opencode_agent_dir),
            "opencode_config_target": str(config_dest),
            "installed": list(PAYLOAD_NAMES), "written": written,
            "opencode_written": opencode_written,
            "opencode_agent_written": opencode_agent_written,
            "opencode_config_written": config_written}


def install_user(cfg: Config) -> dict:
    """Symlink the six payload files into the user commands directory.

    Idempotent: a symlink already pointing at the payload is left alone; a
    symlink pointing anywhere else (a stale install after a package upgrade
    moved ``payload_dir()``) is repointed; a real file at that path that this
    verb didn't create is never touched — refuse instead of clobbering
    whatever a human put there.

    OC-1: also installs an opencode-frontmatter transform of the SAME payload
    into ``opencode_user_commands_dir()``. Unlike the Claude side, this copy
    can't be a symlink (its content is derived, not identical to the source),
    so it has no "is this ours" signal to refuse-to-clobber on — it is kept in
    sync unconditionally, the same posture ``install_repo`` already has for
    both of its copies.

    T-64: also writes a ``--agent`` stub per phase into
    ``opencode_user_agent_dir()`` and one ``opencode_user_config_path()``
    (permission + provider blocks) — same unconditional-sync posture as the
    opencode command copy, and the exact path (``~/.config/opencode/
    opencode.jsonc``) a human had been hand-maintaining before this ticket
    (spec Notes) — this generator now owns it.
    """
    target_dir = user_commands_dir(cfg)
    opencode_dir = opencode_user_commands_dir(cfg)
    opencode_agent_dir = opencode_user_agent_dir(cfg)
    src_dir = payload_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    opencode_dir.mkdir(parents=True, exist_ok=True)
    opencode_agent_dir.mkdir(parents=True, exist_ok=True)

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
    opencode_written = []
    opencode_agent_written = []
    for filename in PAYLOAD_NAMES:
        src = src_dir / filename
        dest = target_dir / filename
        if dest.is_symlink() and Path(os.readlink(dest)) == src:
            pass  # already correct -- leave it alone
        else:
            if dest.is_symlink() or dest.exists():
                dest.unlink()  # stale symlink (or something we already validated isn't a real file)
            dest.symlink_to(src)
            written.append(filename)

        content = src.read_text(encoding="utf-8")
        opencode_dest = opencode_dir / filename
        if _write_if_changed(opencode_dest, _opencode_frontmatter(content)):
            opencode_written.append(filename)
        agent_dest = opencode_agent_dir / filename
        if _write_if_changed(agent_dest, _opencode_agent_stub(_frontmatter_description(content))):
            opencode_agent_written.append(filename)
    config_dest = opencode_user_config_path(cfg)
    config_written = _write_if_changed(config_dest, _dump_opencode_config(cfg))
    return {"target": str(target_dir), "opencode_target": str(opencode_dir),
            "opencode_agent_target": str(opencode_agent_dir),
            "opencode_config_target": str(config_dest),
            "installed": list(PAYLOAD_NAMES), "written": written,
            "opencode_written": opencode_written,
            "opencode_agent_written": opencode_agent_written,
            "opencode_config_written": config_written}
