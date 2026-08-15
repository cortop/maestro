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
    # Cumulative-failure threshold before `ops.fail` dead-letters a ticket into
    # DEGRADED (STALLED + phase change). `failure_count` is a lifetime counter
    # that never resets -- not even when a human revives a DEGRADED ticket
    # (`maestro cmd <KEY> retry` -> READY) -- so this is a ONE-SHOT trip, not a
    # per-attempt retry budget: once a ticket has failed `max_failures` times
    # ever, its very next failure re-dead-letters immediately, with no backoff
    # in between. What it does NOT bound on its own: how many times `ops.fail`
    # itself gets CALLED for an already-degraded ticket -- that was the actual
    # 2026-08-14 runaway (OC-7, T-65's spec Notes: 116 sessions / $8.63 for zero
    # progress). DEGRADED sitting in `statemachine.ACTIVE_PHASES` kept it being
    # respawned every sweep, and each no-progress spawn tripped
    # `max_spawn_attempts`'s watchdog into calling `ops.fail` again -- which,
    # already past this threshold, re-appended another Failed/Stalled pair on
    # every single call, forever, since dead-lettering never sets a backoff
    # timer the way the non-dead-letter branch does. `max_failures` was doing
    # exactly what it says (parking the ticket); the respawn loop feeding it
    # was `statemachine.SLEEPING_PHASES` missing DEGRADED, not this knob -- see
    # the comment there.
    max_failures: int = 4
    max_impl_turns: int = 20               # ralph-loop circuit breaker
    # Watchdog: reap a claim whose session has run past this many seconds (0 disables).
    # Generous by default -- real implementation sessions legitimately run 30-60+ min.
    max_session_seconds: int = 7200
    # Watchdog: fail a key instead of respawning once it has been spawned this many
    # times with observed_seq unchanged (no progress). Resets the moment seq advances.
    max_spawn_attempts: int = 5
    # Watchdog: reap a claim whose session LOG hasn't been written to in this many
    # seconds (0 disables) -- catches a wedged-but-alive session (no output, no
    # error, no exit) long before max_session_seconds would ever trip, since
    # `claims.active_keys` only checks pid-liveness. Independent of the age-based
    # clock above (a fresh no-progress log resets this even past claim epoch age).
    # Exempt when the claim has no `log_path` (capture_session_logs = false --
    # missing data, never reap on it; falls back to the age-based rule only).
    no_output_timeout: int = 600
    # MTO-1: `git worktree add`/adopt's own timeout (worktree_ensure), separate from every
    # other short git plumbing call in ops.py (those stay on the internal 30s _GIT_TIMEOUT).
    # A monorepo checkout (~230k tracked files) measured ~56s; 30s killed it mid-checkout,
    # after files were written but before the index was, leaving a worktree that looked
    # complete (directory present) but wasn't. 600s is generous headroom for that case --
    # raise it further for a larger repo still.
    worktree_timeout: int = 600
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
    # RB-11: threshold shared by `maestro doctor`'s per-key burn WARN (byte-identical
    # `Failed` text, or spawns piling up with `observed_seq` frozen -- see burn.py) AND,
    # if it trips, `burn.should_park`'s dead-letter park -- a per-key rate cap distinct
    # from the fleet-wide `runaway_spawns_per_hour`/`daily_spend_ceiling_usd` gates above
    # (the 2026-08-14 T-55/T-56 incident: $1.40/hr per key, weeks from either ceiling).
    # 0 disables both the WARN and the park.
    burn_repeat_threshold: int = 5
    repo_path: str | None = None           # primary repo the reconciler builds in
    branch_prefix: str = "maestro/"        # branch name prefix for ticket worktrees
    # GA-20: `prime` fallback for the implicit default binding (repos.implicit_default) --
    # lets a single-repo home with no [repos.*] table at all (e.g. this project's own
    # dogfood home) declare a dependency-install command without adopting the multi-repo
    # [repos.<name>] shape. A [repos.<name>] table's own `prime` always wins over this.
    # See RepoBinding.prime (maestro/repos.py) for what runs it, when, and how.
    prime: str | None = None
    # GA-15: override for `maestro install-commands --user` / the doctor check's
    # user-scope fallback. None = ~/.claude/commands (MAESTRO_USER_COMMANDS_DIR
    # env var takes precedence over this when set -- see skills_install.user_commands_dir).
    user_commands_dir: str | None = None
    # OC-1: the opencode counterpart of `user_commands_dir` above -- opencode
    # resolves its own custom commands from a directory it defines, not Claude
    # Code's. None = ~/.config/opencode/command (MAESTRO_OPENCODE_COMMANDS_DIR
    # env var takes precedence -- see skills_install.opencode_user_commands_dir).
    opencode_user_commands_dir: str | None = None
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
    # RF-2: board-wide fallback for the implementation-spawn runner, when a spec carries
    # no `runner:`/`runner_model:` override (dispatcher.resolve_runner). "claude" and
    # "opencode" (OC-4) are the registered runners -- see dispatcher._REGISTERED_RUNNERS.
    runner: str = "claude"
    runner_model: str | None = None
    # OC-3: board-wide kill switch for which runner name(s) `dispatch()` will ever
    # spawn under -- consulted in the spawn loop BEFORE OC-2's own preflight (the
    # binary probe, the daemon probe), so flipping a spec's `runner:` to a name
    # this list doesn't carry never even reaches those probes. A scalar or a list
    # in config.toml (see `_normalize_runner_enabled`); default is claude-only, so
    # an existing home with no `[maestro] runner_enabled` line behaves exactly as
    # before this ticket. "claude" itself is never gated by this knob -- every
    # phase other than `implementing` always spawns claude regardless (RF-2), and
    # gating it here would let one typo'd list wedge the entire fleet.
    runner_enabled: list = field(default_factory=lambda: ["claude"])
    reconcile_web_tools: bool = True   # grant spawned reconcilers WebSearch/WebFetch via --allowedTools
    # GA-10: board-wide --allowedTools additions beyond the maestro-verb grant (cli.py's
    # _AGENT_TOOL_VERBS) and reconcile_web_tools -- e.g. a repo's own git/gh/test surface.
    # Threaded per key through sessions.spawn (see dispatcher.resolved_allowed_tools), unioned
    # with the resolved repo's own [repos.<name>] reconcile_allowed_tools, into ONE
    # --allowedTools flag. Default [] -- today's behavior for every existing home.
    reconcile_allowed_tools: list = field(default_factory=list)
    # Declarative recurring triggers: [[scheduled]] array-of-tables, each a dict with
    # name/prompt/every (+ optional kind/priority/prefix/enabled).
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
    # MTO-2: when dispatcher.sync_worktrees finds an awaiting-ci/in-review ticket's
    # worktree behind its repo's base branch, which policy decides whether that drift
    # alone re-routes it to `implementing` (and so triggers a rebase + force-push,
    # restarting CI). "always" is today's unconditional behavior (byte-identical for a
    # board that sets nothing but this to "always"); "daily" routes at most once per
    # calendar day per ticket (see schedule.dedup_bucket); "on_conflict" never routes on
    # drift alone -- only a GitHub-reported CONFLICTING PR (ops.route_conflict) still
    # rebases. Default "on_conflict": the mode that cannot livelock a fast-moving shared
    # integration branch, which is the entire reason this knob exists -- see MTO-2. A
    # board whose base branch only ever advances on its own merges can opt back into
    # "always" explicitly. Per-[repos.<name>] override wins over this board-wide default,
    # resolved the same way as base_branch/branch_prefix (see repos.RepoBinding). An
    # unrecognized value fails closed at config load (refuse to start), never silently
    # falls back -- see _BASE_DRIFT_POLICIES.
    base_drift_policy: str = "on_conflict"
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
    # RB-12: detection-channel alarm (maestro/alarm.py) -- fires through the SAME
    # transport above (notify_command/webhook_urls) on a runaway-brake auto-pause, a
    # daily-spend warn-threshold crossing, the daily-spend hard ceiling blocking a
    # sweep, or a spawn rate over health.spawn_budget (the exact signal `maestro
    # doctor` calls `runaway`). Own [alarm] table per this ticket's Q&A, deliberately
    # separate from [notify] -- notify.py fires on a KEYED phase transition, this is
    # fleet-wide.
    alarm_spend_warn_fractions: list = field(default_factory=lambda: [0.5, 0.8])
    # Re-announce a still-active brake/spend-hard/spawn-rate condition after this many
    # seconds (the ticket's "within the hour" bar). spend_warn_fractions is the one
    # exception -- those fire at most once per UTC date regardless of this knob.
    alarm_cooldown_s: int = 3600
    raw: dict = field(default_factory=dict)


# GA-17: the whole recognized [repos.<name>] key set -- config.load raises on
# anything outside it (see the fail-closed AC), so a typo'd credential field
# (or any other field) never gets silently ignored.
_REPO_TABLE_KEYS = frozenset({
    "path", "slug", "base_branch", "branch_prefix", "default",
    "max_spawns_per_sweep", "mode", "reconcile_allowed_tools",
    "gh_account", "token_env", "prime", "base_drift_policy",
})

# MTO-2: the whole recognized base_drift_policy value set -- both [maestro] and
# [repos.<name>] fail closed (raise at config.load) on anything outside it,
# naming this set in the error, rather than silently falling back to a mode
# that can livelock a fast-moving base branch.
_BASE_DRIFT_POLICIES = frozenset({"always", "daily", "on_conflict"})

# OC-4/T-54: [runner.opencode]'s whole recognized key set. Unlike every other
# [runner.<name>] table (free-form, riding cfg.provider_config with zero
# config.py awareness -- see the `provider_config` field's own docstring),
# opencode's is explicitly validated here, fail-closed like _REPO_TABLE_KEYS,
# per the spec's Notes ("that passthrough is not fail-closed the way
# _REPO_TABLE_KEYS is, so a typo is silent and silently means wrong model / no
# permission block"). `concurrency` is the per-runner concurrency cap
# (dispatcher._runner_concurrency_cap; default 1 -- see
# dispatcher._RUNNER_DEFAULT_CONCURRENCY for why). `phases` (T-54) is the
# eligible-phase list (dispatcher.runner_eligible_phases; default
# ["implementing"] -- see dispatcher._RUNNER_DEFAULT_PHASES) admitting
# opencode to a phase's spawn beyond just `implementing`. OC-6: `models` (list
# of bare ollama tags, e.g. ["qwen3-coder:30b"]) and `host` (optional
# `OLLAMA_HOST` override) are what `skills_install`'s generated opencode.jsonc
# registers its local "ollama" provider from
# (`skills_install._opencode_provider_block`) -- pinned, explicit config, not
# code scraping ticket specs for whatever model tag happens to be in use.
# `models` unset falls back to the board-wide `runner_model` default (see that
# function's own docstring).
_RUNNER_OPENCODE_KEYS = frozenset({"concurrency", "phases", "models", "host"})


# T-56/T-60: [runner.pi]'s whole recognized key set. Same validation posture as
# _RUNNER_OPENCODE_KEYS (fail-closed at config.load -- see the unknown-key check
# below). `provider` is the name `store.generate_pi_models_json` registers the
# `providers.<name>` block under in the generated models.json (default "zai");
# `base_url`/`api`/`api_key`/`compat` are that provider's own fields (`api_key`
# a resolver expression -- `$ENV_VAR` or `!command ...` -- never a literal, see
# that function's docstring); `models` is a table of `<model id>` ->
# `{context_window, max_tokens, cost: {input, output, cache_read, cache_write}}`;
# `version` is the pinned `pi --version` string `health.check_pi_version` warns
# on drift from. PI-8: `concurrency` (dispatcher._runner_concurrency_cap;
# default 1 -- see dispatcher._RUNNER_DEFAULT_CONCURRENCY) and `phases`
# (dispatcher.runner_eligible_phases; default NONE -- see
# dispatcher._RUNNER_DEFAULT_PHASES's own PI-8 comment on why pi's default is
# deliberately narrower than opencode's) are the same two keys
# _RUNNER_OPENCODE_KEYS already carries.
_RUNNER_PI_KEYS = frozenset({
    "provider", "base_url", "api", "compat", "models", "api_key", "version",
    "concurrency", "phases",
})


def config_path(home: Path) -> Path:
    return home / "config.toml"


def _normalize_runner_enabled(raw, default: list) -> list:
    """OC-3: `[maestro] runner_enabled` accepts either a scalar (`"claude"`) or a
    list (`["claude", "opencode"]`) -- normalize both to a list of str. `None`
    (key absent) keeps *default*; anything else that isn't a str or a list of
    str is ignored (falls back to *default*) rather than wedging `load()` on a
    typo'd table or number."""
    if raw is None:
        return default
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return raw
    return default


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
        cfg.no_output_timeout = int(m.get("no_output_timeout", cfg.no_output_timeout))
        cfg.worktree_timeout = int(m.get("worktree_timeout", cfg.worktree_timeout))
        raw_ceiling = m.get("daily_spend_ceiling_usd", cfg.daily_spend_ceiling_usd)
        cfg.daily_spend_ceiling_usd = float(raw_ceiling) if raw_ceiling is not None else None
        raw_runaway = m.get("runaway_spawns_per_hour", cfg.runaway_spawns_per_hour)
        cfg.runaway_spawns_per_hour = int(raw_runaway) if raw_runaway is not None else None
        cfg.runaway_pause_cooldown = int(
            m.get("runaway_pause_cooldown", cfg.runaway_pause_cooldown))
        cfg.burn_repeat_threshold = int(
            m.get("burn_repeat_threshold", cfg.burn_repeat_threshold))
        cfg.reconcile_command = m.get("reconcile_command", cfg.reconcile_command)
        cfg.repo_path = m.get("repo_path", cfg.repo_path)
        cfg.branch_prefix = m.get("branch_prefix", cfg.branch_prefix)
        cfg.prime = m.get("prime", cfg.prime) or None
        cfg.user_commands_dir = m.get("user_commands_dir", cfg.user_commands_dir)
        cfg.opencode_user_commands_dir = m.get(
            "opencode_user_commands_dir", cfg.opencode_user_commands_dir)
        cfg.user_settings_path = m.get("user_settings_path", cfg.user_settings_path)
        raw_repos = data.get("repos", {})
        if isinstance(raw_repos, dict):
            for name, table in raw_repos.items():
                if not isinstance(table, dict):
                    continue
                # GA-17: ONE fail-closed rule -- an unrecognized key inside
                # [repos.<name>] (e.g. a typo'd credential field) fails config.load
                # loudly rather than being silently ignored, which is exactly how a
                # typo'd `gh_acount` would otherwise leave a binding on the ambient
                # `gh` account with zero feedback. Checked before the `path`-less
                # skip below so a path-less table's typo is still caught.
                unknown = set(table) - _REPO_TABLE_KEYS
                if unknown:
                    raise store.MaestroError(
                        f"config.toml: [repos.{name}] has unrecognized key(s): "
                        f"{', '.join(sorted(unknown))}")
                if not table.get("path"):
                    continue
                raw_cap = table.get("max_spawns_per_sweep")
                raw_mode = table.get("mode", "git")
                raw_repo_tools = table.get("reconcile_allowed_tools", [])
                # MTO-2: fail closed on a typo'd/unrecognized per-repo override too --
                # None (unset) is valid, meaning "inherit the board-wide default"
                # (see repos._binding_from_table).
                raw_repo_drift_policy = table.get("base_drift_policy")
                if raw_repo_drift_policy is not None and raw_repo_drift_policy not in _BASE_DRIFT_POLICIES:
                    raise store.MaestroError(
                        f"config.toml: [repos.{name}] base_drift_policy must be one of "
                        f"{sorted(_BASE_DRIFT_POLICIES)}, got {raw_repo_drift_policy!r}")
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
                    # GA-17: this repo's gh credential -- see maestro/credentials.py.
                    # Both None (default) means "use the ambient gh account", unchanged.
                    "gh_account": table.get("gh_account"),
                    "token_env": table.get("token_env"),
                    # GA-20: this repo's dependency-priming command -- see RepoBinding.prime.
                    "prime": table.get("prime"),
                    # MTO-2: None (unset) inherits cfg.base_drift_policy -- see repos.resolve.
                    "base_drift_policy": raw_repo_drift_policy,
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
        cfg.runner = m.get("runner", cfg.runner)
        cfg.runner_model = m.get("runner_model", cfg.runner_model) or None
        cfg.runner_enabled = _normalize_runner_enabled(m.get("runner_enabled"), cfg.runner_enabled)
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
        raw_drift_policy = m.get("base_drift_policy", cfg.base_drift_policy)
        if raw_drift_policy not in _BASE_DRIFT_POLICIES:
            raise store.MaestroError(
                f"config.toml: base_drift_policy must be one of "
                f"{sorted(_BASE_DRIFT_POLICIES)}, got {raw_drift_policy!r}")
        cfg.base_drift_policy = raw_drift_policy
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
        al = data.get("alarm", {})
        raw_fracs = al.get("spend_warn_fractions", cfg.alarm_spend_warn_fractions)
        cfg.alarm_spend_warn_fractions = (
            [float(x) for x in raw_fracs] if isinstance(raw_fracs, list)
            else cfg.alarm_spend_warn_fractions)
        cfg.alarm_cooldown_s = int(al.get("cooldown_s", cfg.alarm_cooldown_s))
        if "providers" in data:
            cfg.providers.update(data["providers"])
        # OC-4: [runner.opencode] is the one provider_config table this module
        # explicitly validates (fail-closed, same posture as _REPO_TABLE_KEYS) --
        # every other [runner.<name>]/[tracker.*]/[vcs.*]/etc table stays free-form,
        # opaque to config.py, per `provider_config`'s own docstring.
        raw_runner_table = data.get("runner", {})
        if isinstance(raw_runner_table, dict):
            raw_opencode_table = raw_runner_table.get("opencode")
            if isinstance(raw_opencode_table, dict):
                unknown = set(raw_opencode_table) - _RUNNER_OPENCODE_KEYS
                if unknown:
                    raise store.MaestroError(
                        f"config.toml: [runner.opencode] has unrecognized key(s): "
                        f"{', '.join(sorted(unknown))}")
            raw_pi_table = raw_runner_table.get("pi")
            if isinstance(raw_pi_table, dict):
                unknown = set(raw_pi_table) - _RUNNER_PI_KEYS
                if unknown:
                    raise store.MaestroError(
                        f"config.toml: [runner.pi] has unrecognized key(s): "
                        f"{', '.join(sorted(unknown))}")
        cfg.provider_config = {
            k: v for k, v in data.items()
            if k not in {"maestro", "providers"}
        }
        cfg.raw = data
    return cfg


DEFAULT_CONFIG_TOML = """\
# maestro configuration (project-agnostic). Fill in your providers.

[maestro]
max_concurrency = 12              # one COUNTED spawn: sizing the fleet at N concurrent
                                   # sessions can mean slightly more than N agents running
                                   # at once -- a `qa` session fans out ONE extra
                                   # `Agent`-tool Standards-axis sub-agent INSIDE that one
                                   # session when qa_standards_axis is on (see
                                   # health.spawn_rate/spawn_budget, denominated in
                                   # agent-equivalents, not sessions, for exactly this
                                   # reason). The Implementer<->QA loop itself is real,
                                   # separately-counted dispatcher spawns bouncing between
                                   # `implementing` and `qa` (RF-7), not in-session fan-out.
reconcile_steady_interval = 300
# min_spawn_interval = 300        # hard floor between two spawns of the SAME key
                                  # (default: reconcile_steady_interval). Bounds the
                                  # fleet even if the dispatcher is fired too often.
                                  # 0 disables the floor entirely for that key (a
                                  # legitimate debugging mode -- `maestro doctor` warns
                                  # when the effective value is 0). Negative is rejected
                                  # at load (exit 2), not silently clamped to 0.
backoff_base = 30
max_failures = 4                 # lifetime (never-reset) failure count -> dead-letter
                                  # (DEGRADED); a one-shot trip, not a per-attempt retry
                                  # budget -- see the field's docstring in this module.
max_impl_turns = 20
# max_session_seconds = 7200      # kill+fail a claim whose session ran longer than this
                                  # (0 disables). Generous default -- real implementation
                                  # sessions legitimately run 30-60+ min.
# max_spawn_attempts = 5          # fail instead of respawning after this many spawns with
                                  # zero progress (observed_seq unchanged)
# no_output_timeout = 600         # kill+fail a claim whose session LOG hasn't been written
                                  # to in this many seconds (0 disables); independent of
                                  # max_session_seconds above. Exempt when the claim has no
                                  # log_path (capture_session_logs = false).
worktree_timeout = 600           # `git worktree add`/adopt's own timeout (MTO-1); raise this
                                  # for a monorepo whose checkout legitimately takes longer --
                                  # never scale it down, a killed-mid-checkout worktree looks
                                  # complete (dir present) but isn't
daily_spend_ceiling_usd = 150.0  # dispatch() spawns nothing once today's folded
                                  # session spend reaches this (enforced, not advisory;
                                  # surfaced by `maestro doctor` + the TUI fleet panel).
                                  # RB-8: a NEW home must not start uncapped -- an unset
                                  # ceiling silently disarms the fleet's one hard cost
                                  # guard (this exact knob sat unset on this project's own
                                  # dogfood board for the whole period since GA-11 added
                                  # it). 150.0 is this board's own value, set from real
                                  # spend data (~$58/day baseline as of 2026-08-08) --
                                  # generous headroom above a normal day, still an order
                                  # of magnitude below the 2026-07-19 runaway's $845.
                                  # Tune this to your own board's baseline; do not unset it.
# runaway_spawns_per_hour = 200   # `maestro doctor` trips runaway above this fleet-wide
                                  # spawns/hour (default: derived from the spawn floor
                                  # itself; 0 disables the check)
# runaway_pause_cooldown = 900    # seconds dispatch() auto-arms fleet.pause for on the
                                  # same runaway signal doctor reports (0 disables the
                                  # auto-brake; runaway_spawns_per_hour = 0 disables both)
# burn_repeat_threshold = 5       # RB-11: a PER-KEY rate cap, distinct from the fleet-wide
                                  # knobs above -- catches sustained no-progress spend/spawns
                                  # on ONE ticket long before it could ever trip a fleet-wide
                                  # ceiling (measured: $1.40/hr per key, weeks from either).
                                  # `maestro doctor` WARNs once a key repeats byte-identical
                                  # failure text, or piles up spawns with observed_seq frozen,
                                  # this many times; the same threshold parks (dead-letters)
                                  # the key instead of respawning it once its failure text
                                  # repeats. 0 disables both the WARN and the park.
# runner_enabled = ["claude"]     # board-wide kill switch: dispatch() only ever spawns a
                                  # runner named here (default: claude only). A scalar
                                  # ("claude") works too. Consulted before OC-2's own
                                  # preflight (binary probe, daemon probe) -- flipping this
                                  # is the only lever needed to arm/disarm a non-claude
                                  # runner fleet-wide, no spec edits required.
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
# base_drift_policy = "on_conflict" # "always" | "daily" | "on_conflict" -- whether a worktree
                                  # merely BEHIND its base branch (not GitHub-CONFLICTING) alone
                                  # re-routes an awaiting-ci/in-review ticket back to implementing
                                  # (rebase + force-push, restarting CI). Default "on_conflict":
                                  # cannot livelock a fast-moving shared integration branch.
                                  # "always" = today's unconditional behavior; "daily" = at most
                                  # once/calendar-day/ticket. Per-[repos.<name>] override wins.
                                  # Unknown value fails config load closed (see _BASE_DRIFT_POLICIES).
# prime = "python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev,tui]'"
                                  # dependency-install command for the implicit default binding
                                  # (no [repos.*] table at all -- see [repos.<name>] prime below
                                  # for the multi-repo equivalent). Run ONCE per fresh worktree by
                                  # `maestro worktree ensure`, cwd=worktree, with $WT/$REPO/$KEY
                                  # in its environment -- never by the dispatcher.
# qa_standards_axis = true         # spawn a second, parallel QA sub-agent in `qa` that
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
# base_drift_policy = "on_conflict" # this repo's base_drift_policy override -- default: unset,
                                     # inherits [maestro] base_drift_policy. e.g. a fast-moving
                                     # shared integration branch wants "on_conflict" even if the
                                     # board-wide default is "always" for its other, quieter repos.
# gh_account = "work-login"         # resolve this repo's gh credential via
                                     # `gh auth token --user <login>` at spawn/poll time
                                     # (needs `gh` + an unlocked keychain on the dispatcher
                                     # host). token_env below wins when both are set.
# token_env = "GH_TOKEN_DDOG"       # resolve this repo's gh credential from this env var
                                     # instead (deterministic under launchd, no keyring --
                                     # get the var into the LaunchAgent's own environment,
                                     # see maestro/fleet.py). Neither set = ambient gh
                                     # account (today's behavior). Fails closed: a
                                     # configured credential that can't be resolved blocks
                                     # this repo's spawns/polls rather than falling back to
                                     # ambient -- see maestro/credentials.py. Covers `gh`
                                     # API calls only, not `git push` over ssh/osxkeychain.
# prime = "npm ci"                  # dependency-install command run ONCE per fresh worktree
                                     # by `maestro worktree ensure`, cwd=worktree, with
                                     # $WT/$REPO/$KEY in its environment. Read ONLY from this
                                     # file (never a spec/payload field) and run ONLY by the
                                     # reconciler session -- never by the dispatcher. Unset =
                                     # nothing runs (today's behavior). A non-zero exit or a
                                     # timeout fails `worktree ensure` loudly, never silently.

# [runner.opencode]                 # OC-4: opencode's own runner-scoped settings; unknown
                                     # keys here fail config.load (fail-closed, see
                                     # config._RUNNER_OPENCODE_KEYS)
# concurrency = 1                   # cap on concurrently-running opencode reconcilers,
                                     # independent of (and typically tighter than) the
                                     # fleet-wide max_concurrency above -- default 1 until
                                     # measured: opencode wedges intermittently at `init`
                                     # and every process shares one on-disk sqlite db
# phases = ["implementing"]         # T-54: which phase(s) a ticket's `runner: opencode`
                                     # (or this board's own `runner` default) may actually
                                     # spawn under -- default ["implementing"] (unchanged
                                     # from before this key existed); admitting a read-mostly
                                     # phase (e.g. "qa", "triaging", "researching") too is a
                                     # deliberate, reviewable edit here, never a code change
# models = ["qwen3-coder:30b"]      # OC-6: ollama tags to register in the generated
                                     # opencode.jsonc's local "ollama" provider (each pinned
                                     # tool_call = true); unset falls back to [maestro]
                                     # runner_model. `maestro install-commands` is what writes
                                     # the file this actually feeds.
# host = "127.0.0.1:11434"          # OC-6: optional OLLAMA_HOST override for that same
                                     # provider's baseURL; unset resolves the ambient
                                     # OLLAMA_HOST env var same as `maestro runners` does

# [runner.pi]                       # T-56 (PI-4): pi's own maestro-owned agent home --
                                     # unknown keys here fail config.load (fail-closed, see
                                     # config._RUNNER_PI_KEYS). `store.generate_pi_models_json`
                                     # regenerates $PI_CODING_AGENT_DIR/models.json from this
                                     # table before every spawn (sessions.RoutingSessions.spawn,
                                     # its one call site), so no developer's real ~/.pi config
                                     # can ever shadow what a reconciler resolves.
# provider = "zai"                  # the `providers.<name>` key models.json registers under
                                     # (default "zai" -- the vendor whose bundled catalogue this
                                     # ticket's models table extends)
# base_url = "https://api.z.ai/api/coding/paas/v4"
                                     # the Coding-Plan SUBSCRIPTION endpoint -- NOT
                                     # "https://api.z.ai/api/paas/v4" (metered pay-as-you-go);
                                     # the vendor warns the wrong one silently fails to consume
                                     # subscription quota. A config value, never hardcoded.
# api = "openai-completions"        # pi's own wire-protocol name for this provider
# api_key = "$ZAI_API_KEY"          # a RESOLVER EXPRESSION only -- "$ENV_VAR" (interpolated)
                                     # or "!command" (executed, stdout used) -- written through
                                     # verbatim. maestro never reads or holds the secret value.
# version = "0.79.2"                # pinned installed `pi --version`; health.check_pi_version
                                     # WARNs when the real binary drifts from this
# concurrency = 1                   # PI-8: cap on concurrently-running pi reconcilers -- default
                                     # 1: the likely provider's usage policy scopes a subscription
                                     # to 1-2 concurrent projects, with documented account-level
                                     # fair-use enforcement against exactly maestro's fan-out
# phases = ["qa"]                   # PI-8: which phase(s) a ticket's `runner: pi` (or this
                                     # board's own `runner` default) may actually spawn under --
                                     # default is NONE (unlike opencode's ["implementing"]):
                                     # admitting pi to `implementing` is a separate, reviewable
                                     # edit here, made only once the guard has been proven live
#
# [runner.pi.compat]                # the provider-level compat block (pi's own schema);
                                     # mirrors the bundled "zai" entries' own compat so the
                                     # newer model behaves identically to its bundled siblings
# supportsDeveloperRole = false
# thinkingFormat = "zai"
# zaiToolStream = true
#
# [runner.pi.models."glm-5.2"]      # one table per model id this provider registers; context
                                     # window and pricing are the vendor's OWN published figures
                                     # (docs.z.ai/guides/llm/glm-5.2 and
                                     # docs.z.ai/guides/overview/pricing, read 2026-08-15) --
                                     # never copied from this file's comments elsewhere.
                                     # cache_write is NOT pinned to 0 here even though the
                                     # vendor currently lists cache-write/storage as
                                     # "limited-time free": on the measured board, cache writes
                                     # were ~40% of the real bill, and a 0 here would zero the
                                     # spend meter the moment that promo ends. Absent an explicit
                                     # vendor cache-write price, this pins it equal to the input
                                     # rate (the standard assumption when a vendor doesn't
                                     # special-price cache writes) -- revisit if the vendor
                                     # publishes a distinct figure.
# context_window = 1048576          # 1M tokens (docs.z.ai/guides/llm/glm-5.2)
# max_tokens = 128000                # docs.z.ai/guides/llm/glm-5.2's own max-output figure
# reasoning = true                   # GLM-5.2 reasons by default (docs.z.ai/guides/llm/glm-5.2)
                                     # -- omitting this silently drops thinking support, unlike
                                     # every bundled sibling zai model (verified: real
                                     # `pi --list-models` shows "thinking" go from "no" to "yes")
# [runner.pi.models."glm-5.2".cost] # USD per MILLION tokens -- pi's own unit (it divides by
                                     # 1e6 itself, models.js:calculateCost), matching the
                                     # bundled built-in zai entries' units; write the vendor's
                                     # per-million figures here verbatim, no conversion needed
# input = 1.4                       # $1.40 / 1M input tokens (docs.z.ai/guides/overview/pricing)
# output = 4.4                      # $4.40 / 1M output tokens
# cache_read = 0.26                 # $0.26 / 1M cached-input (read) tokens
# cache_write = 1.4                 # not separately published -- pinned to the input rate,
                                     # see the table's own comment above

# [notify]                          # outbound push on awaiting-human/degraded/done (optional)
# notify_command = "terminal-notifier -title maestro -message \"$KEY $PHASE: $QUESTION\""
# webhook_urls = ["https://ntfy.sh/my-maestro-topic"]   # JSON {key,phase,question,title} POSTed

# [alarm]                           # detection-channel alarm (RB-12); reuses [notify]'s
                                     # own notify_command/webhook_urls transport above --
                                     # no [notify] configured means no alarm either
# spend_warn_fractions = [0.5, 0.8] # fire once per UTC date per fraction of
                                     # daily_spend_ceiling_usd crossed
# cooldown_s = 3600                 # re-announce a still-active brake/spend-hard/spawn-rate
                                     # condition after this many seconds (spend-warn is the
                                     # one exception -- always once/day, see above)

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
# runner = "opencode"              # optional; passed through to the minted ticket's runner
# runner_model = "qwen3-coder:30b" # optional; passed through with `runner`
"""

# Field order used when serializing a task back to config.toml (see write_scheduled).
# `title`/`repo` plus every ``schedule.OPTIONAL_MINT_FIELDS`` entry, so the round-trip
# allowlist here can never drift from the mint-args allowlist in dispatcher.py.
# `cron`/`tz` (GA-19) round-trip too but are NOT mint fields -- they configure the
# cadence itself, so they live in the base tuple, not OPTIONAL_MINT_FIELDS.
_SCHEDULED_FIELDS = ("name", "prompt", "every", "cron", "tz", "kind",
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
