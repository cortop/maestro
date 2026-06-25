"""Project-agnostic configuration.

NOTHING about any specific tracker, repo, or workflow is hardcoded in the package.
A project supplies a ``config.toml`` in its MAESTRO_HOME. Providers are named here
and resolved at runtime, so the same maestro core drives Jira+GitHub, Linear+GitLab,
or a pure-local todo list.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import store


@dataclass
class Config:
    home: Path
    max_concurrency: int = 12
    reconcile_steady_interval: int = 300   # seconds between awaiting-ci re-checks
    backoff_base: int = 30                 # seconds; exp backoff on transient failure
    backoff_cap: int = 3600
    max_failures: int = 4                  # -> dead-letter (DEGRADED)
    max_impl_turns: int = 20               # ralph-loop circuit breaker
    daily_token_ceiling: int | None = None # advisory; surfaced by `maestro doctor`
    repo_path: str | None = None           # primary repo the reconciler builds in
    branch_prefix: str = "maestro/"        # branch name prefix for ticket worktrees
    permission_mode: str = "acceptEdits"   # claude permission mode for reconcilers
    reconcile_model: str = "sonnet"        # model for spawned reconciler sessions
    # Provider selection (names resolved by providers/registry).
    providers: dict = field(default_factory=lambda: {
        "tracker": "none",
        "vcs": "none",
        "fetcher": "none",
        "implementer": "claude_skill",
    })
    # Free-form per-provider settings, e.g. provider_config["tracker"]["project_key"].
    provider_config: dict = field(default_factory=dict)
    # Command used to spawn a reconciler session (project may override).
    reconcile_command: str = "/maestro-reconcile"
    capture_session_logs: bool = True
    session_log_format: str = "stream-json"  # "stream-json" | "text"
    nudge_on_human_input: bool = True  # trigger in-process dispatch after ans/cmd/create
    raw: dict = field(default_factory=dict)


def config_path(home: Path) -> Path:
    return home / "config.toml"


def load(home_arg: str | None = None) -> Config:
    home = store.resolve_home(home_arg)
    cfg = Config(home=home)
    path = config_path(home)
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        m = data.get("maestro", {})
        cfg.max_concurrency = int(m.get("max_concurrency", cfg.max_concurrency))
        cfg.reconcile_steady_interval = int(
            m.get("reconcile_steady_interval", cfg.reconcile_steady_interval))
        cfg.backoff_base = int(m.get("backoff_base", cfg.backoff_base))
        cfg.backoff_cap = int(m.get("backoff_cap", cfg.backoff_cap))
        cfg.max_failures = int(m.get("max_failures", cfg.max_failures))
        cfg.max_impl_turns = int(m.get("max_impl_turns", cfg.max_impl_turns))
        cfg.daily_token_ceiling = m.get("daily_token_ceiling", cfg.daily_token_ceiling)
        cfg.reconcile_command = m.get("reconcile_command", cfg.reconcile_command)
        cfg.repo_path = m.get("repo_path", cfg.repo_path)
        cfg.branch_prefix = m.get("branch_prefix", cfg.branch_prefix)
        cfg.permission_mode = m.get("permission_mode", cfg.permission_mode)
        cfg.reconcile_model = m.get("reconcile_model", cfg.reconcile_model)
        cfg.capture_session_logs = bool(m.get("capture_session_logs", cfg.capture_session_logs))
        cfg.session_log_format = m.get("session_log_format", cfg.session_log_format)
        cfg.nudge_on_human_input = bool(m.get("nudge_on_human_input", cfg.nudge_on_human_input))
        if "providers" in data:
            cfg.providers.update(data["providers"])
        cfg.provider_config = {
            k: v for k, v in data.items()
            if k not in {"maestro", "providers"}
        }
        cfg.raw = data
    return cfg


DEFAULT_CONFIG_TOML = """\
# maestro configuration (project-agnostic). Fill in your providers.

[maestro]
max_concurrency = 12
reconcile_steady_interval = 300
backoff_base = 30
max_failures = 4
max_impl_turns = 20
# daily_token_ceiling = 5000000   # advisory cost guardrail

[providers]
tracker = "none"          # "none" | "jira_cli" | "github_issues" | custom
vcs = "none"              # "none" | "github_cli" | custom
fetcher = "none"          # "none" | "command" (runs a shell command to import tickets)
implementer = "claude_skill"

# Example provider settings (uncomment + edit):
# [tracker.jira_cli]
# project_key = "PROJ"
# user = "you@example.com"
#
# [vcs.github_cli]
# repos = ["owner/repo"]
# branch_prefix = "you/"
#
# [fetcher.command]
# cmd = "~/bin/import-tickets.sh"   # writes create-requests to the _new inbox
"""
