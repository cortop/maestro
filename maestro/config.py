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
    session_log_retention_days: int | None = None  # prune logs older than N days; None = keep all
    session_log_max_per_ticket: int | None = None  # keep at most N logs per ticket; None = unlimited
    nudge_on_human_input: bool = True  # trigger in-process dispatch after ans/cmd/create
    research_model: str = "opus"       # model for kind=research tickets
    research_effort: str = "high"      # effort for kind=research tickets
    default_effort: str | None = None  # global effort default; None = omit --effort entirely
    reconcile_web_tools: bool = True   # grant spawned reconcilers WebSearch/WebFetch via --allowedTools
    backup_interval: int = 3600        # seconds between dispatcher auto-backups (0 disables)
    backup_retention: int | None = 24  # keep most-recent N snapshots; 0/None = keep all
    backup_dir: str | None = None      # where snapshots live; None = sibling of the home
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
        raw_days = m.get("session_log_retention_days", cfg.session_log_retention_days)
        cfg.session_log_retention_days = int(raw_days) if raw_days is not None else None
        raw_max = m.get("session_log_max_per_ticket", cfg.session_log_max_per_ticket)
        cfg.session_log_max_per_ticket = int(raw_max) if raw_max is not None else None
        cfg.nudge_on_human_input = bool(m.get("nudge_on_human_input", cfg.nudge_on_human_input))
        cfg.research_model = m.get("research_model", cfg.research_model)
        cfg.research_effort = m.get("research_effort", cfg.research_effort)
        cfg.default_effort = m.get("default_effort", cfg.default_effort) or None
        cfg.reconcile_web_tools = bool(m.get("reconcile_web_tools", cfg.reconcile_web_tools))
        cfg.backup_interval = int(m.get("backup_interval", cfg.backup_interval))
        raw_ret = m.get("backup_retention", cfg.backup_retention)
        cfg.backup_retention = int(raw_ret) if raw_ret is not None else None
        cfg.backup_dir = m.get("backup_dir", cfg.backup_dir)
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
# reconcile_web_tools = true      # grant spawned reconcilers WebSearch/WebFetch via --allowedTools
# backup_interval = 3600          # auto-snapshot events/tickets/inbox/config on this cadence (0 disables)
# backup_retention = 24           # keep this many most-recent snapshots (0 = keep all)
# backup_dir = "~/.maestro/myhome-backups"   # default: a sibling dir of the home

[providers]
tracker = "none"          # "none" | "jira" | "jira_cli" | "github_issues" | custom
vcs = "none"              # "none" | "github_cli" | custom
fetcher = "none"          # "none" | "command" (runs a shell command to import tickets)
implementer = "claude_skill"

# Example provider settings (uncomment + edit):
# [tracker.jira_cli]
# project_key = "PROJ"
# user = "you@example.com"
#
# [tracker.jira]              # REST adapter; opt-in ticket import + tracking sync
# base_url = "https://acme.atlassian.net"
# email = "you@acme.com"
# token_env = "JIRA_API_TOKEN"   # token read from env, never stored in config
# import_jql = "(reporter = currentUser() OR assignee = currentUser()) AND statusCategory = 'To Do'"
# import_fields = ["summary", "description", "status", "issuetype"]
# sync_interval = 900            # seconds between dispatcher sync ticks
#
# [vcs.github_cli]
# repos = ["owner/repo"]
# branch_prefix = "you/"
#
# [fetcher.command]
# cmd = "~/bin/import-tickets.sh"   # writes create-requests to the _new inbox
"""
