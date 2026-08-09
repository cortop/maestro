"""Per-ticket repo binding: which [repos.<name>] a ticket's reconciler builds in.

Zero consumer behavior change (yet) -- nothing reads RepoBinding.resolve() output
to pick a worktree/cwd/slug; that's MR-3/4/5/6. This module only defines the data
model and its precedence so `maestro create --repo` / `maestro env --key` can land
independently. resolve() never raises: an unconfigured name always falls back to
the implicit default so the dispatcher can never wedge on a bad spec edit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import snapshot as snap_mod
from . import store
from .config import Config
from .dispatcher import parse_spec_overrides

_PR_URL_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/pull/\d+")


_MODES = frozenset({"git", "local"})


@dataclass
class RepoBinding:
    name: str
    path: str | None
    slug: str | None
    base_branch: str
    branch_prefix: str
    # MR-5 safety rail: caps how many keys THIS repo may spawn in one dispatcher
    # sweep, independent of max_concurrency/min_spawn_interval/max_spawn_attempts
    # (all still apply on top). None = uncapped -- today's behavior.
    max_spawns_per_sweep: int | None = None
    # AD-6: "git" (default -- worktree/branch/PR, today's behavior) or "local"
    # (a plain directory -- a notes vault, `~/.claude` for self-editing skills --
    # that has no branch/PR path; the reconciler writes in place instead, with
    # backup-on-write as the compensating control for skipping PR review). An
    # unrecognized value falls back to "git" -- never silently write in place
    # from a typo'd config.
    mode: str = "git"
    # GA-10: this repo's own --allowedTools additions (e.g. its git/gh/test surface),
    # unioned with the board-wide Config.reconcile_allowed_tools list -- never a
    # replacement, so [] means "nothing extra beyond board-wide", not "no tools at all".
    reconcile_allowed_tools: list = field(default_factory=list)
    # GA-17: this repo's gh credential -- see maestro/credentials.py. token_env
    # wins when both are set. Neither set (both None, today's default) means
    # "use the ambient gh account", unchanged from before this ticket.
    gh_account: str | None = None
    token_env: str | None = None


def _binding_from_table(name: str, table: dict) -> RepoBinding:
    mode = table.get("mode", "git")
    if mode not in _MODES:
        mode = "git"
    raw_tools = table.get("reconcile_allowed_tools", [])
    return RepoBinding(
        name=name,
        path=table.get("path"),
        slug=table.get("slug"),
        base_branch=table.get("base_branch", "main"),
        branch_prefix=table.get("branch_prefix", "maestro/"),
        max_spawns_per_sweep=table.get("max_spawns_per_sweep"),
        mode=mode,
        reconcile_allowed_tools=raw_tools if isinstance(raw_tools, list) else [],
        gh_account=table.get("gh_account"),
        token_env=table.get("token_env"),
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


def slug_from_pr_url(pr_url: str | None) -> str | None:
    """Parse "owner/repo" out of a GitHub PR URL (e.g.
    "https://github.com/acme/beta/pull/9" -> "acme/beta"). Migration shim: for a
    ticket minted before per-ticket repo binding existed (snapshot.repo unset),
    ``pr_url`` (snapshot.py:44) is the only per-ticket artifact that already
    identifies which repo its PR lives in. Returns None if unset or unparseable.
    """
    if not pr_url:
        return None
    m = _PR_URL_RE.match(pr_url)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def resolve_vcs_slug(cfg: Config, snap: snap_mod.Snapshot) -> str | None:
    """The ``owner/repo`` slug ``dispatcher.sync_vcs``/``_observe_reviews`` should
    pass to the VCS provider for *snap*'s PR: ``[repos.<snap.repo>].slug`` if
    bound to a configured table, else the ``pr_url``-parse shim for pre-binding
    tickets. None means "let the VCS provider fall back to its own default"
    (today's ``repos[0]``/iterate-all behavior), so single-repo boards and
    unbound tickets with no parseable ``pr_url`` are unaffected.
    """
    if snap.repo:
        table = cfg.repos.get(snap.repo)
        if table and table.get("slug"):
            return table["slug"]
    return slug_from_pr_url(snap.pr_url)


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
