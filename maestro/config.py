"""Project-agnostic configuration.

NOTHING about any specific tracker, repo, or workflow is hardcoded in the package.
A project supplies a ``config.toml`` in its MAESTRO_HOME. Providers are named here
and resolved at runtime, so the same maestro core drives Jira+GitHub, Linear+GitLab,
or a pure-local todo list.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import store


@dataclass
class Config:
    home: Path
    max_concurrency: int = 12
    reconcile_steady_interval: int = 300   # seconds between awaiting-ci re-checks
    # Hard floor on how often ONE key may be re-spawned, regardless of why it is due.
    # Independent of claim liveness (a session that dies in <1s frees its claim
    # instantly) and of the launchd cadence, so it still bounds the fleet when the
    # dispatcher is invoked faster than intended. None = fall back to
    # reconcile_steady_interval. Human signals (inbox/spec edit) bypass it.
    min_spawn_interval: int | None = None
    backoff_base: int = 30                 # seconds; exp backoff on transient failure
    backoff_cap: int = 3600
    max_failures: int = 4                  # -> dead-letter (DEGRADED)
    max_impl_turns: int = 20               # ralph-loop circuit breaker
    # Watchdog: reap a claim whose session has run past this many seconds (0 disables).
    # Generous by default -- real implementation sessions legitimately run 30-60+ min.
    max_session_seconds: int = 7200
    # Watchdog: fail a key instead of respawning once it has been spawned this many
    # times with observed_seq unchanged (no progress). Resets the moment seq advances.
    max_spawn_attempts: int = 5
    daily_token_ceiling: int | None = None # advisory; surfaced by `maestro doctor`
    # Fleet-wide spawns/hour above which `maestro doctor` trips `runaway` (exit 1).
    # None = derive from what the spawn-rate floor itself permits (see health.py);
    # 0 disables the check.
    runaway_spawns_per_hour: int | None = None
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
    # Declarative recurring triggers: [[scheduled]] array-of-tables, each a dict with
    # name/prompt/every (+ optional approval_tier/kind/priority/prefix/enabled).
    scheduled: list = field(default_factory=list)
    backup_interval: int = 3600        # seconds between dispatcher auto-backups (0 disables)
    backup_retention: int | None = 24  # keep most-recent N snapshots; 0/None = keep all
    backup_dir: str | None = None      # where snapshots live; None = sibling of the home
    # Fleet-wide rate-limit gate (maestro/ratelimit.py): a rejected rate_limit_event
    # pauses ALL spawns until resets_at + ratelimit_grace, clamped to ratelimit_max_pause.
    ratelimit_grace: int = 60          # seconds added after resetsAt before resuming (clock-skew buffer)
    ratelimit_fallback_pause: int = 1800  # pause length when resetsAt is missing/invalid/past
    ratelimit_max_pause: int = 21600   # cap on any single pause; 0 disables the gate
    # Outbound notify tick: fires on a key's first entry into awaiting-human/degraded/done.
    notify_command: str | None = None  # shell command; KEY/PHASE/QUESTION in env; None = disabled
    webhook_urls: list = field(default_factory=list)  # JSON-POSTed via stdlib urllib
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
        raw_floor = m.get("min_spawn_interval", cfg.min_spawn_interval)
        cfg.min_spawn_interval = int(raw_floor) if raw_floor is not None else None
        cfg.backoff_base = int(m.get("backoff_base", cfg.backoff_base))
        cfg.backoff_cap = int(m.get("backoff_cap", cfg.backoff_cap))
        cfg.max_failures = int(m.get("max_failures", cfg.max_failures))
        cfg.max_impl_turns = int(m.get("max_impl_turns", cfg.max_impl_turns))
        cfg.max_session_seconds = int(m.get("max_session_seconds", cfg.max_session_seconds))
        cfg.max_spawn_attempts = int(m.get("max_spawn_attempts", cfg.max_spawn_attempts))
        cfg.daily_token_ceiling = m.get("daily_token_ceiling", cfg.daily_token_ceiling)
        raw_runaway = m.get("runaway_spawns_per_hour", cfg.runaway_spawns_per_hour)
        cfg.runaway_spawns_per_hour = int(raw_runaway) if raw_runaway is not None else None
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
        raw_scheduled = data.get("scheduled", [])
        cfg.scheduled = raw_scheduled if isinstance(raw_scheduled, list) else []
        cfg.backup_interval = int(m.get("backup_interval", cfg.backup_interval))
        raw_ret = m.get("backup_retention", cfg.backup_retention)
        cfg.backup_retention = int(raw_ret) if raw_ret is not None else None
        cfg.backup_dir = m.get("backup_dir", cfg.backup_dir)
        cfg.ratelimit_grace = int(m.get("ratelimit_grace", cfg.ratelimit_grace))
        cfg.ratelimit_fallback_pause = int(
            m.get("ratelimit_fallback_pause", cfg.ratelimit_fallback_pause))
        cfg.ratelimit_max_pause = int(m.get("ratelimit_max_pause", cfg.ratelimit_max_pause))
        n = data.get("notify", {})
        cfg.notify_command = n.get("notify_command", cfg.notify_command) or None
        raw_webhooks = n.get("webhook_urls", cfg.webhook_urls)
        cfg.webhook_urls = raw_webhooks if isinstance(raw_webhooks, list) else cfg.webhook_urls
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
# min_spawn_interval = 300        # hard floor between two spawns of the SAME key
                                  # (default: reconcile_steady_interval). Bounds the
                                  # fleet even if the dispatcher is fired too often.
backoff_base = 30
max_failures = 4
max_impl_turns = 20
# max_session_seconds = 7200      # kill+fail a claim whose session ran longer than this
                                  # (0 disables). Generous default -- real implementation
                                  # sessions legitimately run 30-60+ min.
# max_spawn_attempts = 5          # fail instead of respawning after this many spawns with
                                  # zero progress (observed_seq unchanged)
# daily_token_ceiling = 5000000   # advisory cost guardrail
# runaway_spawns_per_hour = 200   # `maestro doctor` trips runaway above this fleet-wide
                                  # spawns/hour (default: derived from the spawn floor
                                  # itself; 0 disables the check)
# reconcile_web_tools = true      # grant spawned reconcilers WebSearch/WebFetch via --allowedTools
# backup_interval = 3600          # auto-snapshot events/tickets/inbox/config on this cadence (0 disables)
# backup_retention = 24           # keep this many most-recent snapshots (0 = keep all)
# backup_dir = "~/.maestro/myhome-backups"   # default: a sibling dir of the home
# ratelimit_grace = 60            # seconds added after resetsAt before resuming spawns
# ratelimit_fallback_pause = 1800 # seconds to pause when resetsAt is missing/invalid/past
# ratelimit_max_pause = 21600     # cap on any single pause (0 disables the gate)

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
# sync_interval = 120            # seconds between dispatcher PR/CI/review polls
#
# [fetcher.command]
# cmd = "~/bin/import-tickets.sh"   # writes create-requests to the _new inbox

# [notify]                          # outbound push on awaiting-human/degraded/done (optional)
# notify_command = "terminal-notifier -title maestro -message \"$KEY $PHASE: $QUESTION\""
# webhook_urls = ["https://ntfy.sh/my-maestro-topic"]   # JSON {key,phase,question,title} POSTed

# [[scheduled]]                    # recurring, prompt-defined triggers (optional, repeatable)
# name = "morning-pr-digest"       # stable id -> cursor key + dedup token
# prompt = "Summarize PRs merged to main in the last 24h and open a note ticket."
# every = "24h"                    # "30m" | "6h" | "24h" | a bare integer of seconds
# approval_tier = 1
# kind = "implementation"          # or "research"
# priority = 3
# prefix = "S"                     # minted keys become S-1, S-2, ...
# enabled = true
"""

# Field order used when serializing a task back to config.toml (see write_scheduled).
_SCHEDULED_FIELDS = ("name", "prompt", "every", "approval_tier", "kind", "priority",
                     "prefix", "enabled")
_SCHEDULED_BLOCK_RE = re.compile(r"(?ms)^\[\[scheduled\]\]\n(?:(?!^\[).)*")


def _toml_scalar(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def _serialize_task(task: dict) -> str:
    lines = ["[[scheduled]]"]
    for f in _SCHEDULED_FIELDS:
        val = _toml_scalar(task.get(f))
        if val is None:
            continue
        lines.append(f"{f} = {val}")
    return "\n".join(lines) + "\n"


def write_scheduled(home: Path, tasks: list[dict]) -> None:
    """Rewrite the `[[scheduled]]` array-of-tables in config.toml, leaving every
    other section untouched. The TUI schedule panel's only write path — humans
    editing config.toml by hand still works, this just re-derives the same blocks.
    """
    path = config_path(home)
    text = path.read_text(encoding="utf-8") if path.exists() else DEFAULT_CONFIG_TOML
    text = _SCHEDULED_BLOCK_RE.sub("", text)
    text = text.rstrip("\n") + "\n"
    if tasks:
        text += "\n" + "\n".join(_serialize_task(t) for t in tasks)
    store.atomic_write(path, text if text.endswith("\n") else text + "\n")
