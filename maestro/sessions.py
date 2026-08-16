"""Spawning and listing per-ticket reconciler sessions.

A reconciler is a detached, headless ``claude -p "/maestro-reconcile <KEY>"`` process —
a TOP-LEVEL session (not a subagent), so it keeps the ``Agent`` tool and can run the
Implementer/QA pair. Liveness is tracked via :mod:`maestro.claims` (pid files), because
headless sessions don't show up in ``claude agents --json``.

``SessionManager.list_active`` and ``spawn`` speak in terms of ticket KEYS.
``list_sessions`` enumerates captured log files for a key, sorted newest-first.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Protocol

from . import claims, config, pi_guard, store
from .store import generate_pi_models_json

RECONCILE_PREFIX = "reconcile-"

# OC-6: the opencode provider id `OpencodeCliSessions` composes its `ollama/<tag>`
# --model flag against (see that class's docstring for why the composition itself
# stays in exactly this one place) -- also the provider key
# `skills_install._opencode_provider_block` registers in the generated
# opencode.jsonc, imported from here rather than a second hand-typed "ollama"
# literal so the two can never drift apart.
OPENCODE_MODEL_PROVIDER = "ollama"

# Filename pattern: reconcile-<KEY>-<epoch>.{log,stream.jsonl,opencode.jsonl,pi.jsonl}
_SESSION_FILE_RE = re.compile(
    r"^(reconcile-(?P<key>.+?)-(?P<epoch>\d+\.\d+))\.(?P<ext>log|stream\.jsonl|opencode\.jsonl|pi\.jsonl)$"
)

# Maps a matched filename ``ext`` to the ``format`` value reported to callers. Any
# ``ext`` not listed here falls back to "text" -- there is currently no such case
# (the regex's alternation is closed), but the fallback keeps this mapping additive
# if a future format slot lands. T-58: "pi" is deliberately not "stream-json" --
# see ``store.session_pi_path``'s docstring; every consumer that gates on the exact
# "stream-json" literal (``ratelimit.probe``, ``spend.probe``'s per-candidate check)
# already skips it by construction, same as "opencode".
_EXT_TO_FORMAT = {
    "stream.jsonl": "stream-json",
    "opencode.jsonl": "opencode",
    "pi.jsonl": "pi",
}


def list_sessions(home: Path, key: str, *, with_outcome: bool = False) -> list[dict]:
    """Return session log metadata for *key*, newest-first.

    Each dict has: ``session_id``, ``path``, ``format`` ('text'|'stream-json'|
    'opencode'|'pi'), ``epoch`` (float), ``ts`` (ISO string). ``with_outcome`` is
    opt-in — it tail-scans each log via :func:`maestro.steplog.session_outcome` to
    add an ``outcome`` field, so the default (filename-only) call opens no log
    files, keeping callers like ``ops.prune_session_logs`` and the TUI's log
    tailer cheap.

    'opencode' (RF-3) and 'pi' (T-58) are each a non-Claude runner's own log
    grammar -- deliberately never "stream-json" (``ratelimit.probe`` /
    ``spend.probe`` gate on that exact string to skip logs they can't parse) and
    every consumer above :func:`maestro.steplog.iter_records` that isn't
    format-aware already treats anything other than "stream-json" as opaque text,
    so it falls straight into the existing plain-text render/prune/outcome
    fallbacks.
    """
    store.validate_key(key)
    log_dir = home / "agent-logs" / key
    if not log_dir.exists():
        return []
    out = []
    for f in log_dir.iterdir():
        m = _SESSION_FILE_RE.match(f.name)
        if not m:
            continue
        epoch = float(m.group("epoch"))
        fmt = _EXT_TO_FORMAT.get(m.group("ext"), "text")
        entry = {
            "session_id": m.group(1),
            "path": str(f),
            "format": fmt,
            "epoch": epoch,
            "ts": _epoch_to_iso(epoch),
        }
        if with_outcome:
            from . import steplog
            entry["outcome"] = steplog.session_outcome(f)["outcome"]
        out.append(entry)
    out.sort(key=lambda d: d["epoch"], reverse=True)
    return out


def _epoch_to_iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def session_name(key: str) -> str:
    return f"{RECONCILE_PREFIX}{key}"


class SessionManager(Protocol):
    def list_active(self) -> set[str]:
        """Keys with a live reconciler (the 'already claimed' set)."""

    def spawn(self, key: str, command: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None,
              env_overlay: dict[str, str] | None = None,
              runner: str | None = None,
              runner_model: str | None = None) -> int | None:
        """Launch a detached reconciler for ``key``; return its pid (or None).

        *command* (RF-1) is the resolved reconcile slash-command (see
        ``dispatcher.resolve_reconcile_command``, e.g. ``/maestro-reconcile-implementing``)
        WITHOUT the key appended -- the dispatcher passes ``command`` and ``key`` as separate
        arguments rather than pre-flattening them into one prompt string, so each backend
        composes its own invocation. ``ClaudeCliSessions.spawn`` composes the identical
        ``f"{command} {key}"`` prompt it always has (argv unchanged); a non-Claude backend
        (e.g. opencode) can instead take them as a distinct ``--command <name>`` flag plus
        argument, without string-parsing a slash command back out of an already-flattened
        prompt.

        *model* and *effort* override instance defaults when provided.
        *disallowed_tools* is the merge-denial + per-phase tool-surface denylist
        (see ``dispatcher.MERGE_DENYLIST``/``phase_denylist``) rendered as a
        ``--disallowedTools`` flag.
        *allowed_tools* (GA-10) is the per-key --allowedTools additions
        (``dispatcher.resolved_allowed_tools`` -- the board-wide
        ``reconcile_allowed_tools`` list unioned with the resolved repo binding's
        own list); the implementation merges this with its own process-wide base
        grant (maestro CLI verbs + reconcile_web_tools) into exactly ONE
        ``--allowedTools`` flag, never two.
        *env_overlay* (GA-17) is this key's resolved gh credential (see
        ``maestro.credentials`` / ``dispatcher.resolve_credential``) -- merged
        into the spawned session's env beside the ``MAESTRO_HOME`` pin. None
        (the default -- no credential configured for this key's repo) leaves
        the env byte-identical to before this ticket.

        *runner* (RF-2) is the resolved runner name (``dispatcher.resolve_runner``,
        already validated as registered by the caller) -- an implementation that
        only ever speaks one backend (e.g. ``ClaudeCliSessions``) may ignore it;
        ``RoutingSessions`` uses it to pick which delegate's ``spawn`` to call.
        None means "the default runner" (``"claude"``).

        *runner_model* (OC-4) is the resolved, already-preflighted-ok bare model
        tag for a non-claude runner (the second element ``dispatcher.resolve_runner``
        returns beside *runner* itself) -- ``ClaudeCliSessions`` ignores it (its own
        *model*/*effort* params cover the Claude case); ``OpencodeCliSessions`` is
        the one implementation that actually composes it (prefixed with
        ``ollama/`` -- see its own docstring for why that composition happens
        there and nowhere else).
        """


class ClaudeCliSessions:
    """Real implementation: detached ``claude -p`` + a pid claim file."""

    def __init__(self, home: Path, model: str = "sonnet",
                 permission_mode: str | None = "acceptEdits",
                 extra_args: list[str] | None = None,
                 base_allowed_tools: list[str] | None = None,
                 capture_session_logs: bool = True,
                 session_log_format: str = "stream-json",
                 max_session_turns: int = 0,
                 clock: Callable[[], float] | None = None,
                 unverified_claim_max_age: float = claims.DEFAULT_UNVERIFIED_CLAIM_MAX_AGE,
                 claims_run=subprocess.run):
        self.home = Path(home)
        self.model = model
        self.permission_mode = permission_mode
        self.extra_args = extra_args or []
        # RB-15: 0 (default) omits --max-turns entirely -- byte-identical argv to
        # before this ticket for every existing home (see config.Config.max_session_turns).
        self.max_session_turns = max_session_turns
        # GA-10: the process-wide "always-on" --allowedTools rules (maestro CLI verbs +
        # reconcile_web_tools, see cli._reconciler_tool_grants) -- bare rules, not a
        # pre-built "--allowedTools" pair, so spawn() can merge in the per-key
        # allowed_tools argument and still emit exactly ONE --allowedTools flag.
        self.base_allowed_tools = base_allowed_tools or []
        self.capture_session_logs = capture_session_logs
        self.session_log_format = session_log_format
        self._clock: Callable[[], float] = clock or store.now_epoch
        self._unverified_claim_max_age = unverified_claim_max_age
        self._claims_run = claims_run

    def list_active(self) -> set[str]:
        return claims.active_keys(self.home, run=self._claims_run,
                                  max_age=self._unverified_claim_max_age)

    def spawn(self, key: str, command: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None,
              env_overlay: dict[str, str] | None = None,
              runner: str | None = None,
              runner_model: str | None = None) -> int | None:
        # RF-2: this backend only ever speaks Claude -- `runner` doesn't affect
        # the argv built below (the caller, dispatcher.dispatch, has already
        # validated it's either None or "claude" before routing a spawn to this
        # instance), but T-54 still threads it into the claim written below so
        # `_runner_concurrency_cap`'s active count can be read off the claim
        # instead of re-resolving `resolve_runner` per sweep (see
        # `claims.write_claim`'s own docstring addition).
        # OC-4: `runner_model` is the opencode-shaped bare tag -- meaningless here,
        # this backend's own `model`/`effort` params are what drive a Claude spawn.
        del runner_model
        claimed_runner = runner or "claude"
        session_id = f"{session_name(key)}-{self._clock():.6f}"
        effective_model = model or self.model
        # RF-1: compose the flattened prompt here, from the separate command/key
        # inputs -- identical to the string the caller used to pre-flatten, so argv
        # is unchanged.
        prompt = f"{command} {key}"
        cmd = ["claude", "-p", prompt, "--model", effective_model, "-n", session_name(key)]
        if effort:
            cmd += ["--effort", effort]
        if self.max_session_turns:
            # RB-15: the native, undocumented cap (verified working live against a
            # real `claude -p` invocation) -- the layer of defense a runaway
            # session can't decline, since it never has to call anything to earn
            # it. `run_watchdog`'s max_turn_wallclock_seconds is the backstop for
            # a claude build that drops or ignores this flag.
            cmd += ["--max-turns", str(self.max_session_turns)]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if disallowed_tools:
            cmd += ["--disallowedTools", ",".join(disallowed_tools)]
        # GA-10: merge the process-wide base grant with this key's per-repo/board-wide
        # additions into exactly ONE --allowedTools flag -- never two (an "eval" wrapped
        # around a second flag, or a genuinely duplicated flag, would silently let the
        # LAST one win and could widen the grant unintentionally).
        merged_allowed: list[str] = list(self.base_allowed_tools)
        for tool in allowed_tools or []:
            if tool not in merged_allowed:
                merged_allowed.append(tool)
        assert "--allowedTools" not in self.extra_args, \
            "extra_args must never carry --allowedTools -- use base_allowed_tools instead"
        if merged_allowed:
            cmd += ["--allowedTools", ",".join(merged_allowed)]
        cmd += self.extra_args
        assert cmd.count("--allowedTools") <= 1, "spawn argv must carry at most one --allowedTools flag"
        env = dict(os.environ)
        env["MAESTRO_HOME"] = str(self.home)  # pin the home for the worker
        # GA-17: this key's resolved gh credential wins over whatever's ambient --
        # it's an explicit, already-fail-closed-checked resolution, not a guess.
        # T-56: also where PI_CODING_AGENT_DIR (if RoutingSessions prepared it)
        # rides in -- see that class's spawn() docstring for why it belongs there
        # and not duplicated in each backend.
        if env_overlay:
            env.update(env_overlay)

        log_path: str | None = None
        if self.capture_session_logs:
            if self.session_log_format == "stream-json":
                cmd += ["--output-format", "stream-json", "--verbose"]
                log_file = store.session_stream_path(self.home, key, session_id)
            else:
                log_file = store.session_log_path(self.home, key, session_id)
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_file.open("w", encoding="utf-8")
            log_path = str(log_file)
        else:
            log_handle = None

        # T-52: reserve the claim BEFORE Popen, not after. Writing it after (the
        # old order) left a window where a crash between Popen returning and the
        # write landing left a live, fully-detached (start_new_session=True)
        # reconciler with NO claim -- invisible to max_concurrency and to
        # run_watchdog (which iterates claims only), so the orphan was never
        # reaped. The reservation's pid is THIS process's own -- alive for
        # exactly as long as the launch call takes -- so a crash between the
        # reservation and the real-pid write below self-heals the moment this
        # process dies too, rather than permanently squatting the key's slot.
        # Rolled back (released) if Popen itself raises, so a failed launch
        # never leaves a phantom claim behind either.
        claims.write_claim(self.home, key, os.getpid(), session_name(key),
                            log_path=log_path, cwd=str(cwd), prompt=prompt,
                            runner=claimed_runner)
        try:
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(cwd), env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle if log_handle is not None else subprocess.DEVNULL,
                    stderr=log_handle if log_handle is not None else subprocess.DEVNULL,
                    start_new_session=True,
                )
            finally:
                if log_handle is not None:
                    log_handle.close()
        except Exception:
            claims.release(self.home, key)
            raise

        claims.write_claim(self.home, key, proc.pid, session_name(key),
                            log_path=log_path, cwd=str(cwd), prompt=prompt,
                            runner=claimed_runner)
        return proc.pid


class DryRunSessions:
    """Records spawns instead of launching (for --dry-run and tests)."""

    def __init__(self, active: set[str] | None = None):
        self._active = set(active or set())   # KEYS
        # 10-tuple: (key, prompt, cwd, model, effort, disallowed_tools, allowed_tools,
        # env_overlay, runner, runner_model). GA-10 appended allowed_tools as the 7th
        # element; GA-17 appended env_overlay as the 8th; RF-2 appended runner as the
        # 9th; OC-4 appends runner_model as the 10th -- the SAME way -- any later
        # per-key spawn input appends here too, rather than opening a second per-key
        # channel. Unpack by name or by negative index, never assume this stays
        # exactly 10 long. RF-1: spawn()'s own 2nd parameter is now ``command`` (no
        # key appended) -- this ``prompt`` element is the composed "<command> <key>"
        # string built inside spawn() itself, so existing readers of element 1 see
        # the same value as before this ticket.
        self.spawned: list[tuple[str, str, str, str | None, str | None, list[str], list[str],
                                 dict[str, str], str, str | None]] = []

    def list_active(self) -> set[str]:
        return set(self._active)

    def spawn(self, key: str, command: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None,
              env_overlay: dict[str, str] | None = None,
              runner: str | None = None,
              runner_model: str | None = None) -> int | None:
        prompt = f"{command} {key}"
        self.spawned.append((key, prompt, str(cwd), model, effort,
                             list(disallowed_tools or []), list(allowed_tools or []),
                             dict(env_overlay or {}), runner or "claude", runner_model))
        self._active.add(key)
        return None


class OpencodeCliSessions:
    """OC-4: the second real ``SessionManager`` backend -- speaks the opencode
    CLI instead of the Claude CLI. Reuses ``claims`` unchanged (RF-2's Notes:
    ``claims._verdict`` keys only on pid + ``start_epoch``, never the spawned
    command string, so the claim/watchdog/``max_concurrency`` machinery is
    already runner-agnostic -- there is no excuse to invent a second one here).

    Verified spawn shape (spec Notes): ``opencode run --command <name> --model
    ollama/<bare-tag> --format json --dir <cwd> --agent <name> <key>``.
    opencode's blanket-approve-everything flag (see ``test_runner_permissions.py``'s
    whole-package sweep for the literal it forbids) must NEVER appear in this
    argv -- it inverts opencode's permission model (interactive, ask-by-default
    -> silently approve everything). The ``ollama/`` prefix
    is composed HERE and nowhere else -- *runner_model* (``dispatcher.resolve_runner``'s
    second return value, already validated by OC-2's preflight before a spawn
    ever reaches this method) carries only the bare tag (e.g. ``qwen3-coder:30b``),
    matching what a spec's ``runner_model:`` line and ``[maestro] runner_model``
    both store verbatim. ``<name>`` -- reused for both ``--command`` and
    ``--agent`` -- is *command* (``dispatcher.resolve_reconcile_command``'s
    return value, e.g. ``/maestro-reconcile-implementing``) with its leading
    ``/`` stripped, matching the filename ``skills_install.install_repo``/
    ``install_user`` install under ``.opencode/command/<name>.md`` (OC-1). T-66:
    the ticket key IS passed as a CLI argument -- a trailing positional after
    ``--agent <name>``, exactly like Claude's flattened ``f"{command} {key}"``
    prompt, just split across two argv slots instead of one string. Verified
    empirically (T-66 spec Notes): ``opencode run --command <name> ...`` DOES
    substitute that trailing positional for ``$1`` in the command body, so the
    existing ``$1``/``"$KEY"``-based command files work verbatim under opencode
    with no rewrite. Nothing here depends on *cwd* -- a ``mode: local`` (AD-6)
    ticket, whose cwd is not ``<MHOME>/worktrees/<KEY>``, resolves its key
    exactly the same way.

    Keeps ``start_new_session=True`` (matching ``ClaudeCliSessions``) so pid ==
    pgid -- ``dispatcher.run_watchdog`` kills by pgid, and without this a wedged
    opencode session (spec Notes: opencode wedges intermittently at ``init`` and
    every process shares one on-disk sqlite db) would survive the watchdog as an
    orphan while ``ops.fail`` records it as killed.

    T-64 added the write path this docstring used to say didn't exist yet --
    `maestro install-commands` now writes both a real `.opencode/opencode.jsonc`
    (`skills_install._opencode_declarative_config`: `permission` from
    `runner_permissions.opencode_permission_block`, `provider` from
    `skills_install._opencode_provider_block`) and a `--agent <name>` stub per
    phase (`skills_install._opencode_agent_stub`), so both the `--agent` flag
    below and the bash/external_directory permission block now resolve against
    real, generated files instead of nothing.

    STILL not done here (out of scope for T-64 too -- no AC exercises it, no
    writer exists for it): forbidding sub-agent ("task" tool) nesting via
    opencode's own declarative config. Left for whichever ticket adds that.

    RB-15: this backend has no per-spawn turn-cap flag of its own (`opencode
    run --help` carries none) -- its native raw-turn ceiling instead rides in
    the generated `--agent` stub file itself (`cfg.max_session_turns` ->
    `skills_install._opencode_agent_stub`'s `steps:` field), written once at
    `maestro install-commands` time rather than composed per-spawn here.
    `run_watchdog`'s `max_turn_wallclock_seconds` remains the backstop in case
    a stale-installed stub predates a raised ceiling.
    """

    def __init__(self, home: Path, model_prefix: str = OPENCODE_MODEL_PROVIDER,
                 capture_session_logs: bool = True,
                 clock: Callable[[], float] | None = None,
                 unverified_claim_max_age: float = claims.DEFAULT_UNVERIFIED_CLAIM_MAX_AGE,
                 claims_run=subprocess.run):
        self.home = Path(home)
        self.model_prefix = model_prefix
        self.capture_session_logs = capture_session_logs
        self._clock: Callable[[], float] = clock or store.now_epoch
        self._unverified_claim_max_age = unverified_claim_max_age
        self._claims_run = claims_run

    def list_active(self) -> set[str]:
        # Same claim files, same verified-pid liveness check ClaudeCliSessions
        # uses -- claims are runner-agnostic (see class docstring above).
        return claims.active_keys(self.home, run=self._claims_run,
                                  max_age=self._unverified_claim_max_age)

    def spawn(self, key: str, command: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None,
              env_overlay: dict[str, str] | None = None,
              runner: str | None = None,
              runner_model: str | None = None) -> int | None:
        # This backend only ever speaks opencode; `runner` doesn't affect the
        # argv below (RoutingSessions passes it uniformly). opencode has no
        # equivalent of Claude's --model tier (`model`/`effort`),
        # --disallowedTools, or --allowedTools -- its permission surface is the
        # board-wide, declarative `runner_permissions.opencode_bash_permissions`
        # block instead, not a per-spawn flag -- so those four are accepted (for
        # call-site uniformity through RoutingSessions) and otherwise unused.
        # T-54: `runner` IS still threaded into the claim written below -- see
        # `ClaudeCliSessions.spawn`'s identical comment.
        del model, effort, disallowed_tools, allowed_tools
        claimed_runner = runner or "claude"
        if not runner_model:
            # Must never happen: OC-2's preflight (dispatcher.dispatch) already
            # verified a real, tool-capable runner_model before routing a spawn
            # here. Fail loud rather than silently composing "ollama/None".
            raise store.MaestroError(
                f"OpencodeCliSessions.spawn({key!r}): no runner_model resolved -- "
                "OC-2's preflight must run before a spawn reaches this backend")
        session_id = f"{session_name(key)}-{self._clock():.6f}"
        name = command.lstrip("/")
        cmd = ["opencode", "run",
               "--command", name,
               "--model", f"{self.model_prefix}/{runner_model}",
               "--format", "json",
               "--dir", str(cwd),
               "--agent", name,
               key]

        env = dict(os.environ)
        env["MAESTRO_HOME"] = str(self.home)  # pin the home for the worker
        # T-56: PI_CODING_AGENT_DIR (if RoutingSessions prepared it) rides in via
        # env_overlay -- see that class's spawn() docstring.
        if env_overlay:
            env.update(env_overlay)

        log_path: str | None = None
        if self.capture_session_logs:
            # RF-3: opencode's own log-identity slot -- never "stream-json" (see
            # store.session_opencode_path's docstring).
            log_file = store.session_opencode_path(self.home, key, session_id)
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_file.open("w", encoding="utf-8")
            log_path = str(log_file)
        else:
            log_handle = None

        # T-52: reserve before Popen, commit the real pid after, roll back on a
        # failed launch -- see ClaudeCliSessions.spawn's comment for the full
        # rationale (claims are runner-agnostic, so this backend needs the
        # identical fix).
        claims.write_claim(self.home, key, os.getpid(), session_name(key),
                            log_path=log_path, cwd=str(cwd), prompt=" ".join(cmd),
                            runner=claimed_runner)
        try:
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(cwd), env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle if log_handle is not None else subprocess.DEVNULL,
                    stderr=log_handle if log_handle is not None else subprocess.DEVNULL,
                    start_new_session=True,
                )
            finally:
                if log_handle is not None:
                    log_handle.close()
        except Exception:
            claims.release(self.home, key)
            raise

        claims.write_claim(self.home, key, proc.pid, session_name(key),
                            log_path=log_path, cwd=str(cwd), prompt=" ".join(cmd),
                            runner=claimed_runner)
        return proc.pid


class PiCliSessions:
    """PI-8: the third real ``SessionManager`` backend -- speaks the ``pi``
    coding-agent CLI (``@earendil-works/pi-coding-agent``) instead of the
    Claude CLI. Reuses ``claims`` unchanged, same rationale as
    ``OpencodeCliSessions``'s own docstring (``claims._verdict`` keys only on
    pid + ``start_epoch``, never the spawned command string).

    **The failure mode this backend exists to make impossible is a silent
    no-op** (PI-8 spec Notes): pi's project-local prompt/skill discovery is
    trust-gated, and headless mode shows no trust prompt, so an undiscovered
    ``/maestro-reconcile-implementing <KEY>`` would otherwise be sent to the
    model as literal, unexpanded text -- exit 0, no error, no reconcile step
    taken. The argv built below avoids discovery entirely rather than trying
    to earn trust: ``--no-skills``/``--no-prompt-templates``/``--no-extensions``
    disable every discovered source, and ``--prompt-template <payload dir>``
    (``skills_install.payload_dir()`` -- the SAME installed copy of the
    ``.claude/commands`` payload every other backend reuses, lazy-imported to
    avoid the load-time cycle ``sessions -> skills_install -> sessions``)
    loads the command set explicitly, which is trust-independent (verified
    against the installed pi's compiled resource-loader: the no-discovery
    flags drop only *discovered* prompts, never CLI-supplied paths -- and
    empirically, live, against a real symlinked payload dir in an untrusted
    cwd, see ``tests/test_pi_real_smoke.py``, AC12). pi's own ``$1``
    substitution matches Claude Code's in the existing payload bodies, so the
    same ``/maestro-reconcile-<phase> <KEY>`` flattened prompt every other
    backend sends works verbatim here too, with no payload rewrite.

    **No project-trust flag ever appears** -- ``--approve``/``-a`` would trust
    project-local files for the run, defeating the point of disabling
    discovery above; ``--no-approve`` (pi's own explicit opt-out) is included
    instead, belt-and-suspenders next to non-interactive mode's own default.

    **The guard extension is non-negotiable** (T-59/PI-7): pi is auto-approve
    by design (no sandbox, no permission prompts), so ``pi_guard.spawn_argv``
    -- called here, the same production call site ``providers.pi.fetch_models``
    uses for its own probe subprocess -- installs the destructive-command guard
    extension under this home's maestro-owned pi agent dir
    (``store.pi_agent_dir``) and returns the argv fragment that loads it
    explicitly while disabling all other extension discovery, plus the frozen
    ``--tools`` allowlist (``pi_guard.PI_GUARD_TOOLS``). This method verifies
    the returned extension path actually exists on disk before spawning --
    ``pi_guard.install`` is expected to have just written it, but this method
    never trusts that blindly (an install failure -- a read-only package dir,
    a broken symlink chain -- must fail the spawn loudly, never launch pi
    ungated). Belt-and-suspenders next to ``gates.BYPASS_RESISTANT_IMPLEMENTERS``,
    which refuses this backend a spawn at all until every check pi_guard wires
    in was proven live in production.

    The ``<provider>/`` prefix is composed HERE and nowhere else (matching the
    ``ollama/`` precedent, ``OpencodeCliSessions.spawn`` above) -- against
    ``store.pi_model_provider``, read fresh from this home's ``[runner.pi]``
    config on every spawn (never cached at construction: the same config
    ``RoutingSessions._prep_pi_env`` already reloads per-spawn to regenerate
    ``models.json``, so a provider edit takes effect the very next spawn, not
    just the next process restart). ``--provider`` alone is never emitted --
    the model flag always pairs provider and model in one ``--model
    provider/id`` token, matching ``providers.pi.model_names``'s own
    bare-tag contract for what a spec's ``runner_model:`` line stores.

    T-59 Note 22 (pi auto-loads ``CLAUDE.md``/``AGENTS.md`` from cwd and every
    ancestor regardless of trust): kept ON by default -- a pi reconciler in a
    maestro worktree DOES ingest this very file, written for Claude Code and
    naming tools pi doesn't have. Left on deliberately: pi is confined by
    ``pi_guard``'s own tool-call interception regardless of what a stray
    instruction in ``CLAUDE.md`` asks for (the guard is the actual boundary --
    see that module's docstring), so a misdirected instruction can at worst
    waste a turn, not escape containment; disabling it would also drop the
    repo-specific conventions/write-ownership rules a pi reconciler otherwise
    has no other way to learn. ``disable_context_files=True`` is the opt-out
    knob for a board that judges the ingestion risk differently.

    Keeps ``start_new_session=True`` so pid == pgid -- ``run_watchdog`` kills
    by pgid, and without this a wedged pi session survives as an orphan while
    ``ops.fail`` records it as killed.

    RB-15: unlike ``ClaudeCliSessions``/``OpencodeCliSessions``, this backend
    has NO native raw-turn cap to pass -- checked both ``pi --help`` and the
    compiled CLI's own strings dump, neither shows an argv flag or a config-file
    equivalent. A pi reconciler's turn budget is bounded ENTIRELY by
    ``run_watchdog``'s ``max_turn_wallclock_seconds`` backstop; this method
    intentionally does not read ``cfg.max_session_turns`` at all.
    """

    def __init__(self, home: Path, capture_session_logs: bool = True,
                 disable_context_files: bool = False,
                 clock: Callable[[], float] | None = None,
                 unverified_claim_max_age: float = claims.DEFAULT_UNVERIFIED_CLAIM_MAX_AGE,
                 claims_run=subprocess.run):
        self.home = Path(home)
        self.capture_session_logs = capture_session_logs
        self.disable_context_files = disable_context_files
        self._clock: Callable[[], float] = clock or store.now_epoch
        self._unverified_claim_max_age = unverified_claim_max_age
        self._claims_run = claims_run

    def list_active(self) -> set[str]:
        # Same claim files, same verified-pid liveness check every backend
        # uses -- claims are runner-agnostic (see class docstring above).
        return claims.active_keys(self.home, run=self._claims_run,
                                  max_age=self._unverified_claim_max_age)

    def spawn(self, key: str, command: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None,
              env_overlay: dict[str, str] | None = None,
              runner: str | None = None,
              runner_model: str | None = None) -> int | None:
        # This backend only ever speaks pi; `model`/`effort` are Claude's own
        # tier vocabulary and `disallowed_tools`/`allowed_tools` are Claude's
        # own flag shapes -- none apply here (pi's tool surface is the frozen
        # `pi_guard.PI_GUARD_TOOLS` allowlist instead, not a per-spawn grant).
        # Accepted anyway for call-site uniformity through RoutingSessions,
        # same posture as `OpencodeCliSessions.spawn`.
        del model, effort, disallowed_tools, allowed_tools
        claimed_runner = runner or "claude"
        if not runner_model:
            # Must never happen: OC-2/PI-3's preflight (dispatcher.dispatch)
            # already verified a real, tool-capable runner_model before
            # routing a spawn here. Fail loud rather than silently composing
            # "<provider>/None".
            raise store.MaestroError(
                f"PiCliSessions.spawn({key!r}): no runner_model resolved -- "
                "the preflight must run before a spawn reaches this backend")

        pi_dir = store.pi_agent_dir(self.home)
        pi_config = (config.load(self.home).provider_config.get("runner") or {}).get("pi") or {}
        provider = store.pi_model_provider(pi_config)

        guard_argv = pi_guard.spawn_argv(pi_dir)
        extension_path = Path(guard_argv[guard_argv.index("--extension") + 1])
        if not extension_path.exists():
            # T-59's guard is non-negotiable -- `pi_guard.install` (called by
            # `spawn_argv` above) is expected to have just written this file;
            # never trust that blindly and launch pi ungated if it didn't.
            raise store.MaestroError(
                f"PiCliSessions.spawn({key!r}): guard extension missing at "
                f"{extension_path} -- refusing to spawn pi ungated")

        session_id = f"{session_name(key)}-{self._clock():.6f}"
        # RF-1-equivalent: the flattened prompt, byte-identical in shape to
        # ClaudeCliSessions.spawn's own -- pi's `$1` substitution matches
        # Claude Code's in the existing payload bodies (spec Notes), so this
        # works verbatim with no payload rewrite.
        prompt = f"{command} {key}"

        from . import skills_install  # lazy: avoids the load-time cycle
                                       # sessions -> skills_install -> sessions
                                       # (skills_install imports OPENCODE_MODEL_PROVIDER
                                       # from this module at load time)
        payload_dir = skills_install.payload_dir()

        cmd = ["pi", "-p", prompt,
               "--model", f"{provider}/{runner_model}",
               "--mode", "json",
               "--no-skills",
               "--prompt-template", str(payload_dir),
               "--no-prompt-templates"]
        if self.disable_context_files:
            cmd += ["--no-context-files"]
        cmd += guard_argv

        env = dict(os.environ)
        env["MAESTRO_HOME"] = str(self.home)  # pin the home for the worker
        # T-56: PI_CODING_AGENT_DIR (if RoutingSessions prepared it) rides in
        # via env_overlay -- see RoutingSessions._prep_pi_env's docstring.
        if env_overlay:
            env.update(env_overlay)

        log_path: str | None = None
        if self.capture_session_logs:
            # T-58: pi's own log-identity slot -- never "stream-json" (see
            # store.session_pi_path's docstring).
            log_file = store.session_pi_path(self.home, key, session_id)
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_file.open("w", encoding="utf-8")
            log_path = str(log_file)
        else:
            log_handle = None

        # T-52: reserve before Popen, commit the real pid after, roll back on
        # a failed launch -- see ClaudeCliSessions.spawn's comment for the
        # full rationale (claims are runner-agnostic, so this backend needs
        # the identical fix).
        claims.write_claim(self.home, key, os.getpid(), session_name(key),
                            log_path=log_path, cwd=str(cwd), prompt=prompt,
                            runner=claimed_runner)
        try:
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(cwd), env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle if log_handle is not None else subprocess.DEVNULL,
                    stderr=log_handle if log_handle is not None else subprocess.DEVNULL,
                    start_new_session=True,
                )
            finally:
                if log_handle is not None:
                    log_handle.close()
        except Exception:
            claims.release(self.home, key)
            raise

        claims.write_claim(self.home, key, proc.pid, session_name(key),
                            log_path=log_path, cwd=str(cwd), prompt=prompt,
                            runner=claimed_runner)
        return proc.pid


class RoutingSessions:
    """A ``SessionManager`` that dispatches ``spawn`` to one of several backend
    delegates by runner name (RF-2) -- ``{runner: SessionManager}``. This is the
    ONE place a resolved ``runner`` (``dispatcher.resolve_runner``, already
    validated as registered by the caller -- routing to an unregistered name is
    a caller bug, not a runtime condition this class handles gracefully) turns
    into an actual backend call; both construction sites (``cli.py``'s
    ``_nudge`` and ``cmd_dispatch``) build one of these instead of a bare
    ``ClaudeCliSessions`` now, so a second backend registers by adding one more
    delegate here, not by touching either call site again.

    ``list_active`` asks exactly ONE delegate (the first registered) instead of
    every delegate and merging -- ``claims`` tracks liveness runner-agnostically
    (pid + start_epoch only, never the spawned command string, see
    ``claims._verdict``), so every delegate sharing the same home would compute
    the identical set; asking N of them would just re-read (and, for
    ``ClaudeCliSessions``, re-verify via ``ps``) the same claim files N times.

    T-56: also the ONE production call site that keeps the maestro-owned pi
    agent home (``store.pi_agent_dir``/``store.generate_pi_models_json``) in
    sync with ``[runner.pi]`` config and threads ``PI_CODING_AGENT_DIR`` into
    every spawned reconciler's env -- regardless of which backend actually
    runs it, since a reconciler's own tools can shell out to ``pi`` (a future
    pi runner backend, or an ad hoc call) and must never see a developer's
    real ``~/.pi`` state. Lives here rather than in each backend's own
    ``spawn`` (the pre-T-56 shape) precisely because this is the one place
    every spawn already funnels through, regardless of backend -- duplicating
    it per-backend is what let it drift out of sync with ``provider_config``'s
    real shape the first time. Requires ``home`` to do anything; both real
    construction sites (``cli.py``'s ``_nudge`` and ``cmd_dispatch``) pass
    ``cfg.home``, so this is live in production. Tests that don't exercise pi
    behavior can omit ``home`` and get the pre-T-56 no-op.
    """

    def __init__(self, delegates: dict[str, SessionManager], home: Path | None = None):
        if not delegates:
            raise store.MaestroError("RoutingSessions needs at least one delegate")
        self.delegates = dict(delegates)
        self.home = Path(home) if home is not None else None

    def list_active(self) -> set[str]:
        return next(iter(self.delegates.values())).list_active()

    def spawn(self, key: str, command: str, cwd: Path,
              model: str | None = None, effort: str | None = None,
              disallowed_tools: list[str] | None = None,
              allowed_tools: list[str] | None = None,
              env_overlay: dict[str, str] | None = None,
              runner: str | None = None,
              runner_model: str | None = None) -> int | None:
        name = runner or "claude"
        try:
            delegate = self.delegates[name]
        except KeyError:
            raise store.MaestroError(
                f"RoutingSessions: no delegate registered for runner {name!r} "
                f"(registered: {sorted(self.delegates)}) -- the caller must "
                "validate the runner before calling spawn") from None
        env_overlay = self._prep_pi_env(env_overlay)
        return delegate.spawn(key, command, cwd, model=model, effort=effort,
                              disallowed_tools=disallowed_tools, allowed_tools=allowed_tools,
                              env_overlay=env_overlay, runner=name, runner_model=runner_model)

    def _prep_pi_env(self, env_overlay: dict[str, str] | None) -> dict[str, str] | None:
        """T-56: regenerate models.json and add ``PI_CODING_AGENT_DIR`` to
        *env_overlay* when ``[runner.pi]`` is configured; a no-op passthrough
        otherwise (no ``home``, or nothing configured under ``[runner.pi]``)."""
        if self.home is None:
            return env_overlay
        pi_config = (config.load(self.home).provider_config.get("runner") or {}).get("pi")
        if not isinstance(pi_config, dict):
            return env_overlay
        generate_pi_models_json(self.home, pi_config)
        merged = dict(env_overlay) if env_overlay else {}
        merged.setdefault("PI_CODING_AGENT_DIR", str(store.pi_agent_dir(self.home)))
        return merged
