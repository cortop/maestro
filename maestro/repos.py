"""Per-ticket repo binding: which [repos.<name>] a ticket's reconciler builds in.

Zero consumer behavior change (yet) -- nothing reads RepoBinding.resolve() output
to pick a worktree/cwd/slug; that's MR-3/4/5/6. This module only defines the data
model and its precedence so `maestro create --repo` / `maestro env --key` can land
independently. resolve() never raises: an unconfigured name always falls back to
the implicit default so the dispatcher can never wedge on a bad spec edit.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import snapshot as snap_mod
from . import store
from .config import Config
from .dispatcher import parse_spec_overrides


@dataclass
class RepoBinding:
    name: str
    path: str | None
    slug: str | None
    base_branch: str
    branch_prefix: str


def _binding_from_table(name: str, table: dict) -> RepoBinding:
    return RepoBinding(
        name=name,
        path=table.get("path"),
        slug=table.get("slug"),
        base_branch=table.get("base_branch", "main"),
        branch_prefix=table.get("branch_prefix", "maestro/"),
    )


def implicit_default(cfg: Config) -> RepoBinding:
    """The binding used when no spec/payload names a configured repo.

    Prefers a [repos.<name>] table with `default = true`; otherwise synthesizes
    one from cfg.repo_path/branch_prefix (today's single-repo behavior), with the
    slug taken from the same [vcs.github_cli] repos[0] value providers/cli.py reads.
    """
    for name, table in cfg.repos.items():
        if table.get("default"):
            return _binding_from_table(name, table)
    slug = None
    gh = cfg.provider_config.get("vcs", {}).get("github_cli", {})
    repos_list = gh.get("repos") or []
    if repos_list:
        slug = repos_list[0]
    return RepoBinding(
        name="default",
        path=cfg.repo_path,
        slug=slug,
        base_branch="main",
        branch_prefix=cfg.branch_prefix,
    )


def bound_repo_name(home: Path, key: str) -> str | None:
    """The raw [repos.<name>] name *key* is bound to, before falling back to the
    implicit default: spec frontmatter `repo:` line, else the TicketCreated
    payload's repo (survives a human deleting the frontmatter line, via
    snapshot.repo). None if neither is set. Does not validate the name against
    cfg.repos -- callers that need to distinguish "unbound" from "bound to an
    unconfigured name" (e.g. `maestro env --key`) check that themselves.
    """
    spec_file = store.spec_path(home, key)
    if spec_file.exists():
        overrides = parse_spec_overrides(spec_file.read_text(encoding="utf-8"))
        if overrides.get("repo"):
            return overrides["repo"]
    return snap_mod.load(home, key).repo


def resolve(cfg: Config, home: Path, key: str) -> RepoBinding:
    """Resolve *key*'s repo binding: spec frontmatter > TicketCreated payload > implicit default.

    Never raises -- a name that isn't a configured [repos.<name>] table (e.g. a
    human spec edit naming a typo'd repo) falls back to the implicit default so
    the dispatcher can never wedge.
    """
    name = bound_repo_name(home, key)
    if name:
        table = cfg.repos.get(name)
        if table:
            return _binding_from_table(name, table)
    return implicit_default(cfg)
