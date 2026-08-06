"""Provider registry — resolves config names to adapter instances.

Keeping the core project-agnostic: the engine knows only the abstract interfaces in
``base``. A project picks concrete adapters by name in ``config.toml``.

Trackers are a *keyed set*, not a single choice: ``[providers] tracker`` may name
one tracker (back-compat) or a list of them (e.g. ``["jira", "linear"]``), so Jira
and Linear tickets can be minted and refreshed concurrently. Each ticket remembers
which tracker it came from (``snapshot.external_source``); ``get_trackers`` is what
``dispatcher.sync_external_sources`` looks the right one up in.
"""
from __future__ import annotations

from ..config import Config
from . import base, cli
from . import jira as jira_mod
from . import linear as linear_mod


def _build_tracker(name: str, settings: dict) -> base.Tracker:
    if name == "jira_cli":
        return cli.JiraCliTracker(settings)
    if name == "jira":
        return jira_mod.JiraTracker(settings)
    if name == "linear":
        return linear_mod.LinearTracker(settings)
    return base.NullTracker()


def _tracker_names(cfg: Config) -> list[str]:
    """Names declared by ``[providers] tracker`` — a single string (back-compat) or
    a list, so more than one tracker can be configured at once."""
    raw = cfg.providers.get("tracker", "none")
    names = raw if isinstance(raw, list) else [raw]
    return [n for n in names if n and n != "none"]


def get_trackers(cfg: Config) -> dict[str, base.Tracker]:
    """Every configured tracker, keyed by its config name (e.g. "jira", "linear").
    Empty when ``tracker = "none"`` (the default) or unset."""
    settings_by_name = cfg.provider_config.get("tracker", {})
    return {
        name: _build_tracker(name, settings_by_name.get(name, {}))
        for name in _tracker_names(cfg)
    }


def get_tracker(cfg: Config) -> base.Tracker:
    """Single-tracker convenience: the first configured tracker, or a no-op.
    Prefer ``get_trackers`` wherever more than one tracker may be configured."""
    trackers = get_trackers(cfg)
    if trackers:
        return next(iter(trackers.values()))
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
