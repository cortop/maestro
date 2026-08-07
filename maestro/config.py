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
    # [repos.<name>] tables: name -> {path, slug, base_branch, branch_prefix, default}.
    # Optional -- when empty, repos.resolve() synthesizes an implicit default binding
    # from repo_path/branch_prefix so every existing single-repo config works untouched.
    repos: dict = field(default_factory=dict)
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
    session_log_retention_days: int | None = 14   # prune logs older than N days; 0/None = unlimited
    session_log_max_per_ticket: int | None = 200  # keep at most N logs per ticket; 0/None = unlimited
    prune_interval: int = 3600          # seconds between dispatcher auto-prune ticks (0 disables)
    nudge_on_human_input: bool = True  # trigger in-process dispatch after ans/cmd/create
    research_model: str = "opus"       # model for kind=research tickets
    research_effort: str = "high"      # effort for kind=research tickets
    default_effort: str | None = None  # global effort default; None = omit --effort entirely
    reconcile_web_tools: bool = True   # grant spawned reconcilers WebSearch/WebFetch via --allowedTools
    # Declarative recurring triggers: [[scheduled]] array-of-tables, each a dict with
    # name/prompt/every (+ optional approval_tier/kind/priority/prefix/enabled).
    scheduled: list = field(default_factory=list)
    # Ceiling on how long an "unknown" (unverifiable identity) claim may still be
    # honored via raw pid liveness before it is released with no kill/no event.
    # Generously large so it never races T-13's max_session_seconds watchdog.
    unverified_claim_max_age: int = 24 * 3600
    backup_interval: int = 3600        # seconds between dispatcher auto-backups (0 disables)
    backup_retention: int | None = 24  # keep most-recent N snapshots; 0/None = keep all
    backup_dir: str | None = None      # where snapshots live; None = sibling of the home
    # Refuse to fast-forward/spawn into repo_path while it's mid-merge/rebase or carries
    # a real conflict hunk (see dispatcher.repo_preflight). Fails open on a broken probe.
    repo_preflight: bool = True
    # T-23: gate a second, parallel QA sub-agent in the `implementing` reconcile step that
    # checks CLAUDE.md conventions + a Fowler-smell baseline (the "standards" axis) alongside
    # the existing AD-4 "spec" axis (does the diff satisfy the AC?). Default OFF -- it roughly
    # doubles sub-agent spend per implementing step, and per the T-23 spec a mandatory second
    # axis is explicitly not approved scope. A standards-axis fail is recorded (`maestro
    # qa-verdict --axis standards`) but is advisory only: unlike a spec-axis fail, it does NOT
    # block `set-phase awaiting-ci` (see ops._refuse_if_qa_failing).
    qa_standards_axis: bool = False
    # Maintenance ticks (dispatcher.run_compact_tick / run_archive_tick).
    compact_interval: int = 0          # seconds between dispatcher-driven compact sweeps (0 disables)
    compact_min_events: int = 200      # only compact a key once its folded log reaches this many events
    archive_after: int | None = None   # seconds a DONE ticket stays visible before archive_done moves
                                        # it out of list_keys; None disables the tick, 0 = next sweep
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
        raw_repos = data.get("repos", {})
        if isinstance(raw_repos, dict):
            for name, table in raw_repos.items():
                if not isinstance(table, dict) or not table.get("path"):
                    continue
                raw_cap = table.get("max_spawns_per_sweep")
                raw_mode = table.get("mode", "git")
                cfg.repos[name] = {
                    "path": table["path"],
                    "slug": table.get("slug"),
                    "base_branch": table.get("base_branch", "main"),
                    "branch_prefix": table.get("branch_prefix", cfg.branch_prefix),
                    "default": bool(table.get("default", False)),
                    "max_spawns_per_sweep": int(raw_cap) if raw_cap is not None else None,
                    "mode": raw_mode if raw_mode in ("git", "local") else "git",
                }
        cfg.permission_mode = m.get("permission_mode", cfg.permission_mode)
        cfg.reconcile_model = m.get("reconcile_model", cfg.reconcile_model)
        cfg.capture_session_logs = bool(m.get("capture_session_logs", cfg.capture_session_logs))
        cfg.session_log_format = m.get("session_log_format", cfg.session_log_format)
        raw_days = m.get("session_log_retention_days", cfg.session_log_retention_days)
        cfg.session_log_retention_days = int(raw_days) if raw_days is not None else None
        raw_max = m.get("session_log_max_per_ticket", cfg.session_log_max_per_ticket)
        cfg.session_log_max_per_ticket = int(raw_max) if raw_max is not None else None
        cfg.prune_interval = int(m.get("prune_interval", cfg.prune_interval))
        cfg.nudge_on_human_input = bool(m.get("nudge_on_human_input", cfg.nudge_on_human_input))
        cfg.research_model = m.get("research_model", cfg.research_model)
        cfg.research_effort = m.get("research_effort", cfg.research_effort)
        cfg.default_effort = m.get("default_effort", cfg.default_effort) or None
        cfg.reconcile_web_tools = bool(m.get("reconcile_web_tools", cfg.reconcile_web_tools))
        cfg.unverified_claim_max_age = int(
            m.get("unverified_claim_max_age", cfg.unverified_claim_max_age))
        raw_scheduled = data.get("scheduled", [])
        cfg.scheduled = raw_scheduled if isinstance(raw_scheduled, list) else []
        cfg.backup_interval = int(m.get("backup_interval", cfg.backup_interval))
        raw_ret = m.get("backup_retention", cfg.backup_retention)
        cfg.backup_retention = int(raw_ret) if raw_ret is not None else None
        cfg.backup_dir = m.get("backup_dir", cfg.backup_dir)
        cfg.repo_preflight = bool(m.get("repo_preflight", cfg.repo_preflight))
        cfg.qa_standards_axis = bool(m.get("qa_standards_axis", cfg.qa_standards_axis))
        cfg.compact_interval = int(m.get("compact_interval", cfg.compact_interval))
        cfg.compact_min_events = int(m.get("compact_min_events", cfg.compact_min_events))
        raw_archive_after = m.get("archive_after", cfg.archive_after)
        cfg.archive_after = int(raw_archive_after) if raw_archive_after is not None else None
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
# unverified_claim_max_age = 86400  # ceiling (s) for honoring an unverifiable ("unknown"
                                    # identity) claim by raw pid liveness before releasing it
# backup_interval = 3600          # auto-snapshot events/tickets/inbox/config on this cadence (0 disables)
# backup_retention = 24           # keep this many most-recent snapshots (0 = keep all)
# backup_dir = "~/.maestro/myhome-backups"   # default: a sibling dir of the home
# prune_interval = 3600           # auto-prune stale session logs on this cadence (0 disables)
# session_log_retention_days = 14 # delete session logs older than N days (0/None = keep all)
# session_log_max_per_ticket = 200 # keep at most N session logs per ticket (0/None = unlimited)
# repo_preflight = true            # refuse to spawn/sync into a mid-merge or conflict-marked repo_path
# qa_standards_axis = true         # spawn a second, parallel QA sub-agent in `implementing` that
                                  # checks CLAUDE.md conventions + a Fowler-smell baseline; advisory
                                  # only (does not block awaiting-ci), roughly doubles QA spend
# compact_interval = 21600        # fold pre-snapshot events into the archive on this cadence
                                  # (0 disables; a manual `maestro compact <key>` always works)
# compact_min_events = 200        # skip compacting a key until its folded log reaches this size
# archive_after = 259200          # seconds a DONE ticket stays visible before being moved out of
                                  # list_keys/dashboards (None disables; 0 = archive next sweep)
# ratelimit_grace = 60            # seconds added after resetsAt before resuming spawns
# ratelimit_fallback_pause = 1800 # seconds to pause when resetsAt is missing/invalid/past
# ratelimit_max_pause = 21600     # cap on any single pause (0 disables the gate)

[providers]
tracker = "none"          # "none" | "jira" | "jira_cli" | "linear" | "github_issues" | custom
                          # may also be a list, e.g. ["jira", "linear"], to run more than one
                          # tracker concurrently -- each ticket refreshes against the one it
                          # came from (`external_source`), on its own `sync_interval` cursor
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
# [tracker.linear]             # GraphQL adapter; opt-in ticket import + tracking sync
# api_key_env = "LINEAR_API_KEY"   # personal API key read from env, never stored in config
# import_filter = { assignee = { isMe = { eq = true } }, state = { type = { nin = ["completed", "canceled"] } } }
# sync_interval = 900            # seconds between dispatcher sync ticks
#
# [vcs.github_cli]
# repos = ["owner/repo"]
# branch_prefix = "you/"
# sync_interval = 120            # seconds between dispatcher PR/CI/review polls
#
# [fetcher.command]
# cmd = "~/bin/import-tickets.sh"   # writes create-requests to the _new inbox

# [repos.<name>]                    # optional multi-repo bindings; `maestro create --repo <name>`
# path = "/abs/path/to/repo"        # required
# slug = "owner/repo"               # for the vcs provider (gh, etc.) -- git mode only
# base_branch = "main"              # default: "main" -- git mode only
# branch_prefix = "you/"            # default: [maestro] branch_prefix -- git mode only
# default = true                    # this binding is the implicit default (optional)
# max_spawns_per_sweep = 5          # cap this repo's spawns in ONE dispatcher sweep (optional;
                                     # default: uncapped). Composes with, never replaces,
                                     # min_spawn_interval/max_spawn_attempts/the fleet-wide rate gate.
# mode = "local"                    # "git" (default: worktree/branch/PR) or "local" -- a plain
                                     # directory (a notes vault, `~/.claude` for self-editing
                                     # skills) with no branch/PR path; the reconciler writes in
                                     # place, backing up the target first (`maestro local-backup`).

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
