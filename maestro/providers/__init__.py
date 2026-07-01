"""Provider registry — resolves config names to adapter instances.

Keeping the core project-agnostic: the engine knows only the abstract interfaces in
``base``. A project picks concrete adapters by name in ``config.toml``.
"""
from __future__ import annotations

from ..config import Config
from . import base, cli
from . import jira as jira_mod


def get_tracker(cfg: Config) -> base.Tracker:
    name = cfg.providers.get("tracker", "none")
    settings = cfg.provider_config.get("tracker", {}).get(name, {})
    if name == "jira_cli":
        return cli.JiraCliTracker(settings)
    if name == "jira":
        return jira_mod.JiraTracker(settings)
    return base.NullTracker()


def get_vcs(cfg: Config) -> base.VCS:
    name = cfg.providers.get("vcs", "none")
    settings = cfg.provider_config.get("vcs", {}).get(name, {})
    if name == "github_cli":
        return cli.GitHubCliVCS(settings)
    return base.NullVCS()


def get_fetcher(cfg: Config) -> base.Fetcher:
    name = cfg.providers.get("fetcher", "none")
    settings = cfg.provider_config.get("fetcher", {}).get(name, {})
    if name == "command":
        return cli.CommandFetcher(settings)
    return base.NullFetcher()
