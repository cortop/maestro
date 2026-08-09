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

from . import schedule, store


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
    # GA-11: enforced (not advisory) fleet-wide daily spend ceiling, folded from
    # session logs' `total_cost_usd` by maestro/spend.py. None = no ceiling. Surfaced
    # by `maestro doctor` and the TUI fleet panel alongside today's actual spend.
    daily_spend_ceiling_usd: float | None = None
    # Fleet-wide spawns/hour above which `maestro doctor` trips `runaway` (exit 1).
    # None = derive from what the spawn-rate floor itself permits (see health.py);
    # 0 disables the check.
    runaway_spawns_per_hour: int | None = None
    # GA-5: seconds `dispatch()` arms `fleet.pause` for when it observes the same
    # `runaway` condition `maestro doctor` reports (health.spawn_rate(...)["total"]
    # > health.spawn_budget(cfg)) -- the auto-brake beside the fleet-wide rate-limit
    # gate. 0 disables the auto-brake while leaving doctor's advisory intact;
    # `runaway_spawns_per_hour = 0` disables both (spawn_budget() returns 0).
    runaway_pause_cooldown: int = 900
    repo_path: str | None = None           # primary repo the reconciler builds in
    branch_prefix: str = "maestro/"        # branch name prefix for ticket worktrees
    # GA-15: override for `maestro install-commands --user` / the doctor check's
    # user-scope fallback. None = ~/.claude/commands (MAESTRO_USER_COMMANDS_DIR
    # env var takes precedence over this when set -- see skills_install.user_commands_dir).
    user_commands_dir: str | None = None
    # GA-16: override for the doctor permission-surface check's user-scope settings
    # layer (Claude Code resolves permissions across a repo's settings.local.json/
    # settings.json AND this file). None = ~/.claude/settings.json
    # (MAESTRO_USER_SETTINGS_PATH env var takes precedence over this when set --
    # see health.user_settings_path). Injectable so no test ever reads a
    # developer's real ~/.claude/settings.json.
    user_settings_path: str | None = None
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
    # GA-10: board-wide --allowedTools additions beyond the maestro-verb grant (cli.py's
    # _AGENT_TOOL_VERBS) and reconcile_web_tools -- e.g. a repo's own git/gh/test surface.
    # Threaded per key through sessions.spawn (see dispatcher.resolved_allowed_tools), unioned
    # with the resolved repo's own [repos.<name>] reconcile_allowed_tools, into ONE
    # --allowedTools flag. Default [] -- today's behavior for every existing home.
    reconcile_allowed_tools: list = field(default_factory=list)
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
        if raw_floor is not None and int(raw_floor) < 0:
            # GA-8: 0 is a legitimate, documented single-key debugging mode (it disables
            # the floor -- see spawn_floor()); a negative value is just a typo and is
            # worse than 0 (it would silently clamp to 0 with zero feedback), so fail
            # closed at load rather than let it through. dispatch()/doctor never even
            # build a Config from this home, matching the fencing-gated log's "loud, not
            # silent" posture -- see cli.main's `except store.MaestroError` (exit 2).
            raise store.MaestroError(
                f"config.toml: min_spawn_interval must be >= 0, got {raw_floor!r}")
        cfg.min_spawn_interval = int(raw_floor) if raw_floor is not None else None
        cfg.backoff_base = int(m.get("backoff_base", cfg.backoff_base))
        cfg.backoff_cap = int(m.get("backoff_cap", cfg.backoff_cap))
        cfg.max_failures = int(m.get("max_failures", cfg.max_failures))
        cfg.max_impl_turns = int(m.get("max_impl_turns", cfg.max_impl_turns))
        cfg.max_session_seconds = int(m.get("max_session_seconds", cfg.max_session_seconds))
        cfg.max_spawn_attempts = int(m.get("max_spawn_attempts", cfg.max_spawn_attempts))
        raw_ceiling = m.get("daily_spend_ceiling_usd", cfg.daily_spend_ceiling_usd)
        cfg.daily_spend_ceiling_usd = float(raw_ceiling) if raw_ceiling is not None else None
        raw_runaway = m.get("runaway_spawns_per_hour", cfg.runaway_spawns_per_hour)
        cfg.runaway_spawns_per_hour = int(raw_runaway) if raw_runaway is not None else None
        cfg.runaway_pause_cooldown = int(
            m.get("runaway_pause_cooldown", cfg.runaway_pause_cooldown))
        cfg.reconcile_command = m.get("reconcile_command", cfg.reconcile_command)
        cfg.repo_path = m.get("repo_path", cfg.repo_path)
        cfg.branch_prefix = m.get("branch_prefix", cfg.branch_prefix)
        cfg.user_commands_dir = m.get("user_commands_dir", cfg.user_commands_dir)
        cfg.user_settings_path = m.get("user_settings_path", cfg.user_settings_path)
        raw_repos = data.get("repos", {})
        if isinstance(raw_repos, dict):
            for name, table in raw_repos.items():
                if not isinstance(table, dict) or not table.get("path"):
                    continue
                raw_cap = table.get("max_spawns_per_sweep")
                raw_mode = table.get("mode", "git")
                raw_repo_tools = table.get("reconcile_allowed_tools", [])
                cfg.repos[name] = {
                    "path": table["path"],
                    "slug": table.get("slug"),
                    "base_branch": table.get("base_branch", "main"),
                    "branch_prefix": table.get("branch_prefix", cfg.branch_prefix),
                    "default": bool(table.get("default", False)),
                    "max_spawns_per_sweep": int(raw_cap) if raw_cap is not None else None,
                    "mode": raw_mode if raw_mode in ("git", "local") else "git",
                    # GA-10: unset (absent from the table) inherits the board-wide
                    # reconcile_allowed_tools list -- this is unioned in, never a replacement,
                    # so [] here means "nothing extra beyond board-wide", not "no tools at all".
                    "reconcile_allowed_tools": raw_repo_tools if isinstance(raw_repo_tools, list) else [],
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
        raw_allowed = m.get("reconcile_allowed_tools", cfg.reconcile_allowed_tools)
        cfg.reconcile_allowed_tools = raw_allowed if isinstance(raw_allowed, list) else cfg.reconcile_allowed_tools
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
                                  # 0 disables the floor entirely for that key (a
                                  # legitimate debugging mode -- `maestro doctor` warns
                                  # when the effective value is 0). Negative is rejected
                                  # at load (exit 2), not silently clamped to 0.
backoff_base = 30
max_failures = 4
max_impl_turns = 20
# max_session_seconds = 7200      # kill+fail a claim whose session ran longer than this
                                  # (0 disables). Generous default -- real implementation
                                  # sessions legitimately run 30-60+ min.
# max_spawn_attempts = 5          # fail instead of respawning after this many spawns with
                                  # zero progress (observed_seq unchanged)
# daily_spend_ceiling_usd = 50.0  # dispatch() spawns nothing once today's folded
                                  # session spend reaches this (enforced, not advisory;
                                  # surfaced by `maestro doctor` + the TUI fleet panel)
# runaway_spawns_per_hour = 200   # `maestro doctor` trips runaway above this fleet-wide
                                  # spawns/hour (default: derived from the spawn floor
                                  # itself; 0 disables the check)
# runaway_pause_cooldown = 900    # seconds dispatch() auto-arms fleet.pause for on the
                                  # same runaway signal doctor reports (0 disables the
                                  # auto-brake; runaway_spawns_per_hour = 0 disables both)
# reconcile_web_tools = true      # grant spawned reconcilers WebSearch/WebFetch via --allowedTools
# reconcile_allowed_tools = ["Bash(npm test:*)"]   # board-wide --allowedTools additions, unioned
                                  # with the resolved repo's own [repos.<name>]
                                  # reconcile_allowed_tools (default [] -- no extra grant)
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
# reconcile_allowed_tools = ["Bash(npm test:*)"]   # this repo's own git/gh/test --allowedTools
                                     # surface; unset inherits just the board-wide list (unioned,
                                     # never replaces it)

# [notify]                          # outbound push on awaiting-human/degraded/done (optional)
# notify_command = "terminal-notifier -title maestro -message \"$KEY $PHASE: $QUESTION\""
# webhook_urls = ["https://ntfy.sh/my-maestro-topic"]   # JSON {key,phase,question,title} POSTed

# [[scheduled]]                    # recurring, prompt-defined triggers (optional, repeatable)
# name = "morning-pr-digest"       # stable id -> cursor key + dedup token
# prompt = "Summarize PRs merged to main in the last 24h and open a note ticket."
# every = "24h"                    # "30m" | "6h" | "24h" | a bare integer of seconds --
                                    # exactly one of every/cron is required, never both
# cron = "0 9 * * 1"               # 5-field cron (minute hour dom month dow); wall-clock
                                    # cadence instead of an elapsed-seconds interval, e.g.
                                    # "Monday 09:00" rather than "604800 seconds from
                                    # whenever it last fired"
# tz = "America/New_York"          # IANA zone the cron fields are evaluated in; default
                                    # "UTC" -- UTC never has a DST transition and never
                                    # depends on the host, so a task that never sets this
                                    # can't hit either DST edge below. Resolved via
                                    # datetime.fromtimestamp(now, ZoneInfo(tz)), never the
                                    # machine's local wall clock (that would make a laptop
                                    # crossing timezones, or a launchd job whose TZ differs
                                    # from your shell, silently move the schedule). An
                                    # unknown/unloadable zone is rejected at add/edit time,
                                    # never falls back to UTC silently at fire time.
                                    # Spring-forward (a local instant that doesn't exist)
                                    # fires at the next valid instant; fall-back (a local
                                    # instant that occurs twice) fires exactly once.
# approval_tier = 1
# kind = "implementation"          # or "research"
# priority = 3
# prefix = "S"                     # minted keys become S-1, S-2, ...
# enabled = true
# title = "Morning PR digest"      # optional; falls back to `name` when unset
# repo = "alpha"                   # optional; unconfigured names still mint (WARNed by `doctor`)
# model = "sonnet"                 # optional; passed through to the minted ticket
# effort = "high"                  # optional; passed through to the minted ticket
# notes = "Skip weekends."         # optional; passed through to the minted ticket's ## Notes
# depends_on = ["T-1"]             # optional; passed through to the minted ticket's dependsOn
"""

# Field order used when serializing a task back to config.toml (see write_scheduled).
# `title`/`repo` plus every ``schedule.OPTIONAL_MINT_FIELDS`` entry, so the round-trip
# allowlist here can never drift from the mint-args allowlist in dispatcher.py.
# `cron`/`tz` (GA-19) round-trip too but are NOT mint fields -- they configure the
# cadence itself, so they live in the base tuple, not OPTIONAL_MINT_FIELDS.
_SCHEDULED_FIELDS = ("name", "prompt", "every", "cron", "tz", "approval_tier", "kind",
                     "priority", "prefix", "enabled", "title") + schedule.OPTIONAL_MINT_FIELDS
# Matches a `[[scheduled]]` header plus ONLY its immediately-following contiguous
# `key = value` lines -- never blank lines, comments, or another table header.
# `_serialize_task` never emits a blank line or comment inside one task's block,
# so this always swallows a machine-written block whole, but stops the instant it
# hits anything a human added around/between blocks (GA-13 Part C: the old
# `(?:(?!^\[).)*` body ran all the way to the next `[`-prefixed line, eating any
# comment written inside or after a trailing scheduled block).
_SCHEDULED_BLOCK_RE = re.compile(
    r"(?m)^\[\[scheduled\]\]\n(?:^[A-Za-z_][A-Za-z0-9_]*[ \t]*=[^\n]*\n?)*")


def _toml_scalar(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        # depends_on is the only list-valued field today. Round-trip it as a real
        # TOML array of strings -- never fall through to str(v), which would
        # serialize a Python list repr as a quoted string and silently corrupt it
        # on the next TUI write (see the correction in GA-9's spec).
        if not v:
            return None
        return "[" + ", ".join(_toml_scalar(x) for x in v) + "]"
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
    other section — and every comment outside a scheduled block's key/value
    lines (GA-13 Part C) — untouched. The single write path behind
    `ops.schedule_*`, used by both the CLI `schedule` verbs and the TUI schedule
    panel; humans editing config.toml by hand still works, this just re-derives
    the same blocks.

    Symlink-safe (GA-13 Part B): writes via `store.atomic_write(...,
    follow_symlinks=True)`, an opt-in kept scoped to this one call site rather
    than made the default for `atomic_write`'s ~26 other callers — see that
    function's docstring for the full rationale. A symlinked config.toml keeps
    its symlink; the write lands in the target file.
    """
    path = config_path(home)
    text = path.read_text(encoding="utf-8") if path.exists() else DEFAULT_CONFIG_TOML
    text = _SCHEDULED_BLOCK_RE.sub("", text)
    text = text.rstrip("\n") + "\n"
    if tasks:
        text += "\n" + "\n".join(_serialize_task(t) for t in tasks)
    store.atomic_write(path, text if text.endswith("\n") else text + "\n",
                        follow_symlinks=True)
