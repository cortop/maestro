"""The ``maestro`` command — the single interface for humans and agents.

Humans use a tiny verb set (create / ans / cmd / status / show). Agents inside a
reconcile session use the state verbs (snapshot / fold-inbox / set-phase / ask /
fail / requeue / finalize) — every one correct-by-construction. The dispatcher
(launchd) calls ``dispatch``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from . import backup, claims, event_log, events, fleet, health, inbox, ops, projection, ratelimit, repos as repos_mod, schedule, skills_install, snapshot as snap_mod, steplog, store
from . import dispatcher as disp
from .config import Config, DEFAULT_CONFIG_TOML, config_path, load
from .sessions import ClaudeCliSessions, DryRunSessions, list_sessions
from .statemachine import Phase

HOME_DIRS = ["events", "inbox", "tickets", "worktrees",
             "derived/snapshots", "derived/cursors", "derived/claims", "derived/context",
             "agent-logs"]


def _cfg(args) -> Config:
    return load(getattr(args, "home", None))


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str) if not isinstance(obj, str) else obj)


# Verbs a spawned reconciler may invoke via the maestro CLI, rendered into
# `Bash(maestro <verb>:*)` rules by `_reconciler_tool_grants` below. Kept as an
# explicit constant rather than derived from build_parser()'s "[agent]" help
# tags at runtime -- reading argparse's per-choice help strings means reaching
# into a private API (`_SubParsersAction._choices_actions`), fine for a test's
# drift guard but too fragile to depend on in production code. See
# tests/test_web_tools.py::test_agent_grant_matches_cli_agent_tags, which walks
# build_parser() with that private API and fails if this list and the
# "[agent]"-tagged verbs there ever drift apart.
#
# NEVER add "approve" (self-clears the tier-2 gate AD-1 exists to enforce),
# "restore" (the one irreversible verb), "fleet" (launchd + the pause kill
# switch), or any other human-only verb here -- and never collapse this to
# the bare wildcard "maestro:*", which grants all of those at once.
_AGENT_TOOL_VERBS = (
    # The 19 "[agent]"-tagged verbs registered in build_parser().
    "local-backup", "snapshot", "events", "append", "set-phase", "ask",
    "fold-inbox", "inbox-ack", "observe-spec", "requeue", "fail", "impl-turn",
    "verify-ac", "qa-verdict", "finalize", "release", "check-conflicts",
    "check-merged", "fold-steps",
    # Not "[agent]"-tagged, but genuinely invoked by skills (grep skills/*.md):
    "env",     # every phase preamble's first command, all phase files
    "show",    # maestro-reconcile-passive.md reads pending_inbox through it
    "create",  # maestro-reconcile-awaiting-human.md mints implementation tickets
)


def _reconciler_tool_grants(cfg: Config) -> list[str]:
    """The process-wide, "always-on" --allowedTools rules for spawned reconcilers.

    Always grants the maestro CLI verbs in _AGENT_TOOL_VERBS -- unconditionally,
    not gated behind reconcile_web_tools (that used to also disarm a
    reconciler's own bookkeeping whenever web tools were turned off). Adds
    WebSearch/WebFetch when reconcile_web_tools is enabled. Per-verb rules are
    prefix matches on the literal command string, so this only ever matches
    spawns that omit --home (they do -- the home is pinned via the
    MAESTRO_HOME env var instead, sessions.py).

    Returns the bare rule list, NOT a "--allowedTools <value>" pair -- GA-10's
    per-key reconcile_allowed_tools (dispatcher.resolved_allowed_tools) merges
    into this same list at spawn time (ClaudeCliSessions.base_allowed_tools),
    so there is exactly one --allowedTools flag in the final argv, never two.
    """
    rules = [f"Bash(maestro {verb}:*)" for verb in _AGENT_TOOL_VERBS]
    if cfg.reconcile_web_tools:
        rules += ["WebSearch", "WebFetch"]
    return rules


def _nudge(cfg: Config) -> disp.DispatchReport:
    """In-process dispatch sweep after a human-input verb (ans/cmd/create).

    Spawns are detached Popen so this returns quickly. The existing per-key
    claim dedup prevents double-spawning if a reconciler is already live.
    Returns the report so callers can react (e.g. a paused-fleet notice); every
    call site inherits that notice for free since the print lives here.
    """
    sessions = ClaudeCliSessions(
        cfg.home, model=cfg.reconcile_model,
        permission_mode=cfg.permission_mode,
        base_allowed_tools=_reconciler_tool_grants(cfg),
        capture_session_logs=cfg.capture_session_logs,
        session_log_format=cfg.session_log_format,
        unverified_claim_max_age=cfg.unverified_claim_max_age,
    )
    report = disp.dispatch(cfg, sessions, now=store.now_epoch())
    if report.repo_blockers:
        if cfg.repos:
            # Named [repos.*] tables configured -- attribute each warning to its
            # own repo (a bound repo's tickets are the only ones actually gated).
            for name, blockers in report.repo_blockers_by_repo.items():
                print(f"warning: repo '{name}' is blocked ({'; '.join(blockers)}) "
                      "— no reconciler spawned for its tickets", file=sys.stderr)
        else:
            # Back-compat: a single-repo board (no [repos.*] tables) keeps the
            # exact pre-MR-5 message, unattributed.
            print(f"warning: repo_path is blocked ({'; '.join(report.repo_blockers)}) "
                  "— no reconciler spawned", file=sys.stderr)
    if report.paused:
        print("fleet is paused — queued, will run on resume")
    return report


# --- lifecycle / human verbs -------------------------------------------------
def cmd_init(args) -> int:
    cfg = _cfg(args)
    for d in HOME_DIRS:
        (cfg.home / d).mkdir(parents=True, exist_ok=True)
    cp = config_path(cfg.home)
    if not cp.exists():
        store.atomic_write(cp, DEFAULT_CONFIG_TOML)
    _print(f"initialized maestro home at {cfg.home}")
    return 0


_SEED_SPEC_TEMPLATE = """\
# {title}

<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->

approval_tier: {tier}
priority: {priority}
dependsOn: []

## Intent
{intent}

## Acceptance criteria
- [ ] \
"""


def _prompt(prompt_text: str, default: str = "") -> str:
    """Prompt user with optional default; return stripped input or default on empty."""
    display = f"{prompt_text} [{default}]: " if default else f"{prompt_text}: "
    raw = input(display).strip()
    return raw if raw else default


def _editor_intent(title: str, tier: int, priority: int) -> str | None:
    """Open $EDITOR with a pre-filled seed spec; return intent text on save."""
    import os
    import tempfile
    editor = os.environ.get("EDITOR", "").strip()
    if not editor:
        return None
    seed = _SEED_SPEC_TEMPLATE.format(title=title, tier=tier, priority=priority, intent="")
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
        f.write(seed)
        tmp = f.name
    os.system(f"{editor} {tmp}")
    with open(tmp) as f:
        content = f.read()
    os.unlink(tmp)
    intent_lines = []
    in_intent = False
    for line in content.splitlines():
        if line.strip() == "## Intent":
            in_intent = True
            continue
        if in_intent:
            if line.startswith("## "):
                break
            intent_lines.append(line)
    return "\n".join(intent_lines).strip() or None


def _stdin_intent() -> str | None:
    """Fallback: read multi-line intent from stdin (end with a blank line)."""
    print("Intent (blank line to finish):")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines).strip() or None


def cmd_create(args) -> int:
    cfg = _cfg(args)
    title = args.title

    if title is None:
        if not sys.stdin.isatty():
            print(
                "error: the following arguments are required: title\n"
                "To create interactively, run 'maestro create' from a terminal.",
                file=sys.stderr,
            )
            return 2
        # Interactive guided flow
        try:
            title = _prompt("Title")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 0
        if not title:
            print("Aborted (empty title).")
            return 0
        try:
            tier_str = _prompt("Approval tier", str(args.tier))
            priority_str = _prompt("Priority", str(args.priority))
            key_str = _prompt("Key (blank = auto)", "")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 0
        try:
            tier = int(tier_str)
        except ValueError:
            tier = args.tier
        try:
            priority = int(priority_str)
        except ValueError:
            priority = args.priority
        key = key_str.strip() or None
        try:
            intent = _editor_intent(title, tier, priority)
            if intent is None:
                intent = _stdin_intent()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 0
        a: dict = {"approval_tier": tier, "priority": priority}
        if intent:
            a["intent"] = intent
        inbox.append_new(cfg.home, title, key=key, args=a)
        _print(f"queued create: {key or '(auto-key)'} — {title}")
        if not args.no_nudge and cfg.nudge_on_human_input:
            _nudge(cfg)
        return 0

    if getattr(args, "repo", None) and args.repo not in cfg.repos:
        names = ", ".join(sorted(cfg.repos)) or "(none configured)"
        print(f"error: unknown repo '{args.repo}' — configured repos: {names}",
              file=sys.stderr)
        return 2

    a = {"approval_tier": args.tier, "priority": args.priority}
    if args.intent:
        a["intent"] = args.intent
    if getattr(args, "kind", None):
        a["kind"] = args.kind
    if getattr(args, "repo", None):
        a["repo"] = args.repo
    if getattr(args, "model", None):
        a["model"] = args.model
    if getattr(args, "effort", None):
        a["effort"] = args.effort
    if getattr(args, "notes", None):
        a["notes"] = args.notes
    depends_on = getattr(args, "depends_on", None) or []
    if depends_on:
        a["depends_on"] = depends_on
    inbox.append_new(cfg.home, title, key=args.key, args=a,
                     prefix=getattr(args, "prefix", None))
    _print(f"queued create: {args.key or '(auto-key)'} — {title}")
    if not args.no_nudge and cfg.nudge_on_human_input:
        _nudge(cfg)
    return 0


def cmd_ans(args) -> int:
    cfg = _cfg(args)
    a = {"text": args.text}
    if args.qid:
        a["qid"] = args.qid
    inbox.append_command(cfg.home, args.key, "ans", a)
    _print(f"answer queued for {args.key}")
    if not args.no_nudge and cfg.nudge_on_human_input:
        _nudge(cfg)
    return 0


def cmd_approve(args) -> int:
    """[human] clear the tier-2 implementing gate; ticket is due next sweep."""
    cfg = _cfg(args)
    ops.approve(cfg, args.key, actor="human")
    _print(f"approved {args.key}")
    if not args.no_nudge and cfg.nudge_on_human_input:
        _nudge(cfg)
    return 0


def cmd_answer(args) -> int:
    """Interactive walkthrough: find every open question and record answers."""
    cfg = _cfg(args)
    if not sys.stdin.isatty():
        print(
            "maestro answer requires an interactive terminal.\n"
            "To answer non-interactively, use:\n"
            "  maestro ans <KEY> \"<answer>\" [--qid <qid>]",
            file=sys.stderr,
        )
        return 1

    keys = [args.key] if args.key else disp.list_keys(cfg.home)
    waiting_phases = {Phase.AWAITING_HUMAN.value, Phase.DEGRADED.value}

    queue: list[tuple[str, str, str]] = []  # (key, qid, question_text)
    for key in sorted(keys, key=disp.split_key):
        s = snap_mod.load(cfg.home, key)
        if s.phase in waiting_phases and s.open_questions:
            for qid, text in s.open_questions.items():
                queue.append((key, qid, text))

    if not queue:
        print("Nothing waiting for your input.")
        return 0

    total = len(queue)
    answered = 0
    for i, (key, qid, text) in enumerate(queue):
        remaining = total - i
        print(f"\n[{key}] ({remaining} remaining)")
        print(f"  {text}")
        try:
            raw = input("  Answer (Enter/s=skip, q=quit): ").strip()
        except EOFError:
            break
        if raw in ("q", "quit"):
            break
        if raw in ("", "s", "skip"):
            continue
        inbox.append_command(cfg.home, key, "ans", {"qid": qid, "text": raw})
        answered += 1

    print(f"\n{answered} answered, {total - answered} remaining.")
    return 0


def cmd_cmd(args) -> int:
    cfg = _cfg(args)
    inbox.append_command(cfg.home, args.key, args.command,
                         {"text": " ".join(args.rest)} if args.rest else {})
    _print(f"command '{args.command}' queued for {args.key}")
    if not args.no_nudge and cfg.nudge_on_human_input:
        _nudge(cfg)
    return 0


def cmd_status(args) -> int:
    cfg = _cfg(args)
    keys = disp.list_keys(cfg.home)
    counts: dict[str, int] = {}
    waiting = []
    for k in keys:
        s = snap_mod.load(cfg.home, k)
        counts[s.phase] = counts.get(s.phase, 0) + 1
        if s.phase in {Phase.AWAITING_HUMAN.value, Phase.DEGRADED.value}:
            waiting.append((k, s.phase, list(s.open_questions.values())))
        elif disp.needs_approval(cfg.home, k, s):
            # Not a phase (still "implementing") -- the second field carries the
            # *reason* it needs you instead, so it's distinguishable from a real
            # phase value at a glance, same vocabulary as `is_due`'s own reason.
            waiting.append((k, "needs-approval", []))
    _print({"tickets": len(keys), "by_phase": counts, "needs_you": waiting})
    return 0


def cmd_show(args) -> int:
    cfg = _cfg(args)
    snap = snap_mod.rebuild(cfg.home, args.key)
    evs = event_log.read(cfg.home, args.key)
    _print({"snapshot": snap.to_dict(),
            "events": evs[-args.tail:] if args.tail else evs,
            "pending_inbox": inbox.pending(cfg.home, args.key)})
    return 0


def cmd_doctor(args) -> int:
    cfg = _cfg(args)
    now = store.now_epoch()
    rpt = health.report(cfg, now)
    rpt["rate_limit"] = ratelimit.status(cfg.home, now)
    rpt["repo_preflight"] = disp.repo_preflight(cfg)
    _print(rpt)
    return 1 if rpt["runaway"] else 0


# --- dispatcher / projection (launchd) --------------------------------------
def cmd_dispatch(args) -> int:
    cfg = _cfg(args)
    if args.dry_run:
        sessions = DryRunSessions()
    else:
        sessions = ClaudeCliSessions(
            cfg.home, model=args.model or cfg.reconcile_model,
            permission_mode=cfg.permission_mode,
            base_allowed_tools=_reconciler_tool_grants(cfg),
            session_log_format=cfg.session_log_format,
            unverified_claim_max_age=cfg.unverified_claim_max_age)
    report = disp.dispatch(cfg, sessions, now=store.now_epoch(), dry_run=args.dry_run)
    projection.write(cfg.home)
    out = {"minted" if not args.dry_run else "would_mint":
               report.minted if not args.dry_run else report.would_mint,
           "spawned" if not args.dry_run else "would_spawn": report.spawned,
           "claimed": report.claimed, "capacity_skipped": report.capacity_skipped,
           "throttled": report.throttled,
           "active_sessions": report.active_sessions,
           "scheduled_fired": report.scheduled_fired,
           "pruned_logs": report.pruned_logs,
           "pruned_bytes": report.pruned_bytes,
           "errors": report.errors,
           "paused_until": report.paused_until,
           "reaped": report.reaped,
           "due": [{"key": k, "reason": r} for k, r in report.due],
           "paused": report.paused,
           "repo_blocked": report.repo_blockers,
           "repo_blocked_by_repo": report.repo_blockers_by_repo,
           "worktree_removal_errors": report.worktree_removal_errors}
    _print(out)
    return 0


def cmd_ratelimit(args) -> int:
    """Show (or clear) the fleet-wide rate-limit pause set by maestro/ratelimit.py."""
    cfg = _cfg(args)
    if args.clear:
        _print({"cleared": ratelimit.clear(cfg.home)})
        return 0
    _print(ratelimit.status(cfg.home, store.now_epoch()))
    return 0


_SCHEDULE_TASK_FLAGS = ("prompt", "every", "cron", "tz", "kind", "approval_tier", "priority",
                        "prefix", "title", "repo", "model", "effort", "notes",
                        "depends_on", "enabled")


def cmd_schedule(args) -> int:
    """[[scheduled]] tasks: `list` (read-only, the historic default) plus the
    write actions add/edit/rm/enable/disable, all funneled through
    `ops.schedule_*` -- this function itself does no load/mutate/write of
    config.toml, only translates argv into the ops call.
    """
    cfg = _cfg(args)
    if args.action == "list":
        rows = disp.schedule_status(cfg, store.now_epoch())
        _print({"scheduled": rows})
        return 0
    if not args.name:
        print(f"error: schedule {args.action} requires a task name", file=sys.stderr)
        return 2
    try:
        if args.action == "add":
            if not args.prompt or (not args.every and not args.cron):
                print("error: schedule add requires --prompt and exactly one of --every/--cron",
                      file=sys.stderr)
                return 2
            task = {"name": args.name, "prompt": args.prompt, "every": args.every,
                    "cron": args.cron, "tz": args.tz,
                    "kind": args.kind or "implementation",
                    "approval_tier": args.approval_tier if args.approval_tier is not None else 1,
                    "priority": args.priority if args.priority is not None else 3,
                    "enabled": args.enabled if args.enabled is not None else True}
            for field in ("prefix", "title", "repo", "model", "effort", "notes", "depends_on"):
                task[field] = getattr(args, field)
            result = ops.schedule_add(cfg, task)
        elif args.action == "edit":
            updates = {f: getattr(args, f) for f in _SCHEDULE_TASK_FLAGS if getattr(args, f) is not None}
            result = ops.schedule_edit(cfg, args.name, updates)
        elif args.action == "rm":
            result = ops.schedule_remove(cfg, args.name)
        elif args.action == "enable":
            result = ops.schedule_set_enabled(cfg, args.name, True)
        else:  # "disable"
            result = ops.schedule_set_enabled(cfg, args.name, False)
    except store.MaestroError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print(result)
    return 0


def cmd_project(args) -> int:
    cfg = _cfg(args)
    written = projection.write(cfg.home)
    _print({"wrote": written})
    return 0


def _parse_until(value: str) -> float:
    """A bare epoch (int/float) or an ISO-8601 string (naive = local time)."""
    try:
        return float(value)
    except ValueError:
        pass
    return datetime.fromisoformat(value).timestamp()


def cmd_fleet(args) -> int:
    cfg = _cfg(args)
    if args.action == "up":
        _print(fleet.up(cfg.home, interval=args.interval))
    elif args.action == "down":
        _print(fleet.down(cfg.home))
    elif args.action == "pause":
        until = None
        if args.until:
            until = _parse_until(args.until)
        elif args.for_:
            until = store.now_epoch() + schedule.parse_every(args.for_)
        _print(fleet.pause(cfg.home, until=until, reason=args.reason))
    elif args.action == "resume":
        _print(fleet.resume(cfg.home))
    else:
        _print(fleet.status(cfg.home))
    return 0


# --- agent state verbs (used inside a reconcile session) --------------------
def cmd_snapshot(args) -> int:
    _print(snap_mod.rebuild(_cfg(args).home, args.key).to_dict())
    return 0


def cmd_events(args) -> int:
    _print(event_log.read(_cfg(args).home, args.key, since=args.since))
    return 0


# Types owned by an ops verb -- a raw `maestro append` of one of these would
# bypass the invariant that verb enforces (a ceiling, a gate, evidence
# validation, an audit trail). Denylist, not allowlist: `events.SIDE_EFFECTING`
# (PrOpened/PrUpdated/Finalized/QuestionAsked) and ad-hoc types like Note or
# ResearchProposed record an action that already happened and stay appendable.
_APPEND_DENYLIST = {
    events.IMPL_TURN: "impl-turn",
    events.PHASE_CHANGED: "set-phase",
    events.APPROVED: "approve",
    events.AC_VERIFIED: "verify-ac",
    events.AC_QA_VERDICT: "qa-verdict",
    events.FAILED: "fail",
    events.STALLED: "fail",
}


def cmd_append(args) -> int:
    verb = _APPEND_DENYLIST.get(args.type)
    if verb is not None:
        raise store.MaestroError(
            f"maestro append: {args.type!r} is ops-owned -- use `maestro {verb}` instead")
    cfg = _cfg(args)
    payload = json.loads(args.payload) if args.payload else {}
    ev = event_log.append(cfg.home, args.key, args.type, payload, actor=args.actor,
                          step_id=args.step_id,
                          expected_last_seq=args.expect)
    snap_mod.rebuild(cfg.home, args.key)
    _print(ev or {"noop": "idempotent (step_id already applied)"})
    return 0


def cmd_set_phase(args) -> int:
    cfg = _cfg(args)
    ev = ops.set_phase(cfg, args.key, Phase(args.phase), reason=args.reason or "",
                       actor=args.actor, requeue_in=args.requeue, force=args.force)
    _print(ev or {"noop": "phase already set"})
    return 0


def cmd_ask(args) -> int:
    cfg = _cfg(args)
    if args.questions:
        if args.text:
            raise store.MaestroError(
                "maestro ask: pass either TEXT or --question (repeatable), not both")
        triples = [(text, recommend or None, qid or None) for text, recommend, qid in args.questions]
        qids = ops.ask_round(cfg, args.key, triples, actor=args.actor)
        _print({"asked": qids})
        return 0
    if not args.text:
        raise store.MaestroError("maestro ask: TEXT or --question is required")
    qid = ops.ask(cfg, args.key, args.text, qid=args.qid, actor=args.actor)
    _print({"asked": qid})
    return 0


def cmd_fold_inbox(args) -> int:
    folded = ops.fold_inbox(_cfg(args), args.key)
    _print({"folded": folded})
    return 0


def cmd_inbox_ack(args) -> int:
    n = inbox.ack(_cfg(args).home, args.key)
    _print({"cursor": n})
    return 0


def cmd_observe_spec(args) -> int:
    _print({"spec_hash": ops.observe_spec(_cfg(args), args.key, actor=args.actor)})
    return 0


def cmd_requeue(args) -> int:
    ops.requeue(_cfg(args), args.key, args.seconds, actor=args.actor)
    _print({"requeued_in_s": args.seconds})
    return 0


def cmd_fail(args) -> int:
    _print({"result": ops.fail(_cfg(args), args.key, args.error, actor=args.actor)})
    return 0


def cmd_impl_turn(args) -> int:
    """[agent] record one implementing turn; parks the ticket if this crosses max_impl_turns."""
    _print(ops.record_impl_turn(_cfg(args), args.key, role=args.role, actor=args.actor))
    return 0


def cmd_check_conflicts(args) -> int:
    """Route a CONFLICTING PR back to implementing for auto-resolution (idempotent)."""
    if args.state != "CONFLICTING":
        _print({"conflict": False, "reason": f"mergeable={args.state}"})
        return 0
    routed = ops.route_conflict(_cfg(args), args.key, args.pr_number, actor=args.actor)
    _print({"conflict": True, "routed_to": "implementing", "moved": routed})
    return 0


def cmd_verify_ac(args) -> int:
    """[agent] attest AC #n with structured evidence — content-hash keyed, idempotent."""
    evidence = {"what": args.what, "where": args.where, "result": args.result}
    h = ops.verify_ac(_cfg(args), args.key, args.ac, evidence, actor=args.actor)
    _print({"verified_ac_hash": h})
    return 0


def cmd_qa_verdict(args) -> int:
    """[agent] record an independent QA verdict (pass/fail) for AC #n with evidence,
    on either the "spec" axis (default; gates awaiting-ci) or the T-23 "standards"
    axis (advisory only, config-gated by qa_standards_axis)."""
    h = ops.record_qa_verdict(_cfg(args), args.key, args.ac, args.verdict, args.evidence,
                              axis=args.axis, actor=args.actor)
    _print({"qa_verdict_ac_hash": h, "verdict": args.verdict, "axis": args.axis})
    return 0


def cmd_finalize(args) -> int:
    cfg = _cfg(args)
    ops.finalize(cfg, args.key, actor=args.actor)
    _print({"finalized": args.key})
    return 0


def cmd_check_merged(args) -> int:
    """Finalize if the GitHub PR state is MERGED; no-op otherwise."""
    finalized = ops.check_merged(_cfg(args), args.key, args.state, actor=args.actor)
    _print({"finalized": finalized})
    return 0


def cmd_compact(args) -> int:
    _print(ops.compact(_cfg(args), args.key))
    return 0


def cmd_archive_done(args) -> int:
    cfg = _cfg(args)
    moved = ops.archive_done(cfg, after=args.after, now=store.now_epoch())
    _print({"archived": moved})
    return 0


def cmd_why(args) -> int:
    """The recent per-sweep dispatcher decisions recorded for one key, from the
    `derived/dispatch.jsonl` ledger's tail -- what made it due/skipped/spawned,
    and any hook errors on those sweeps."""
    cfg = _cfg(args)
    _print({"key": args.key,
            "decisions": disp.key_decisions(cfg.home, args.key, tail=args.tail)})
    return 0


def cmd_release(args) -> int:
    """A reconciler calls this on exit to drop its claim (best-effort)."""
    claims.release(_cfg(args).home, args.key)
    _print({"released": args.key})
    return 0


def cmd_claims(args) -> int:
    """List claim files with verified identity: key/pid/age/verdict; --purge drops
    the denied and over-age ones (leaves confirmed claims on disk)."""
    cfg = _cfg(args)
    rows = claims.describe_claims(cfg.home, max_age=cfg.unverified_claim_max_age)
    if args.purge:
        dropped = [r["key"] for r in rows if not r["claimed"]]
        for key in dropped:
            claims.release(cfg.home, key)
        _print({"purged": dropped})
    else:
        _print(rows)
    return 0


def cmd_fold_steps(args) -> int:
    """Fold notable stream steps from a session log into IMPL_STEP events."""
    cfg = _cfg(args)
    if args.log:
        n = steplog.fold_stream(cfg.home, args.key, Path(args.log))
    else:
        n = steplog.fold_current_session(cfg.home, args.key)
    _print({"folded_steps": n})
    return 0


def _render_result_line(obj: dict) -> str:
    classified = steplog.classify_result(obj)
    dur = obj.get("duration_ms")
    suffix = f" ({dur}ms)" if dur else ""
    outcome = classified["outcome"]
    if outcome == "success":
        return f"[result:{classified['subtype']}]{suffix}"
    parts = [f"[result:{outcome}]"]
    if classified["api_error_status"] is not None:
        parts.append(f"api_error_status={classified['api_error_status']}")
    if classified["message"]:
        parts.append(classified["message"])
    return " ".join(parts) + suffix


def _render_rate_limit_line(obj: dict) -> str:
    info = obj.get("rate_limit_info") or {}
    kind = info.get("rateLimitType", "")
    status = info.get("status", "")
    resets_at = steplog.format_resets_at(info.get("resetsAt"))
    return f"[rate_limit:{kind}] status={status} resetsAt={resets_at}"


def _render_stream_jsonl(path: Path) -> None:
    """Print a human-readable view of a stream-jsonl session log."""
    seen_msg_ids: dict[str, dict] = {}
    with path.open(encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "assistant":
                mid = obj["message"]["id"]
                seen_msg_ids[mid] = obj
            elif obj.get("type") == "rate_limit_event":
                print(_render_rate_limit_line(obj))
            elif obj.get("type") == "result":
                # Flush collected assistant messages in order, then show result
                for _, msg_obj in seen_msg_ids.items():
                    _print_assistant_message(msg_obj)
                seen_msg_ids.clear()
                print(_render_result_line(obj))
    # Flush any remaining (live/incomplete session)
    for _, msg_obj in seen_msg_ids.items():
        _print_assistant_message(msg_obj)


def _print_assistant_message(obj: dict) -> None:
    for block in obj["message"].get("content", []):
        btype = block.get("type")
        if btype == "text":
            print(block["text"])
        elif btype == "tool_use":
            inp = block.get("input", {})
            inp_preview = json.dumps(inp)[:120]
            print(f"[tool_use:{block['name']}] {inp_preview}")


def cmd_logs(args) -> int:
    cfg = _cfg(args)
    key = getattr(args, "key_flag", None) or args.key
    if not key:
        print("error: a ticket key is required (positional or --key)", file=sys.stderr)
        return 2
    if args.list:
        _print(list_sessions(cfg.home, key, with_outcome=True))
        return 0

    sessions = list_sessions(cfg.home, key)
    if not sessions:
        print(f"No session logs found for {key}.", file=sys.stderr)
        return 1

    if args.session:
        matches = [s for s in sessions if s["session_id"] == args.session]
        if not matches:
            print(f"Session {args.session!r} not found.", file=sys.stderr)
            return 1
        sess = matches[0]
    else:
        sess = sessions[0]  # newest

    log_path = Path(sess["path"])
    is_stream = sess["format"] == "stream-json"

    if args.follow:
        # Tail the file; stop when the session process is gone OR its identity is
        # denied (a reused pid claiming to be this session would otherwise poll
        # pid_alive() forever, since the reused process really is alive).
        claim = claims.read_claim(cfg.home, key)
        live_pid = claim.get("pid") if claim else None
        verdict = claims.verify_claim(cfg.home, key) if claim else "unknown"
        with log_path.open(encoding="utf-8", errors="replace") as f:
            buf = ""
            while True:
                chunk = f.read(4096)
                if chunk:
                    buf += chunk
                    if is_stream and not args.json:
                        # Emit complete lines only, rendered human-readable
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if obj.get("type") == "assistant":
                                _print_assistant_message(obj)
                            elif obj.get("type") == "rate_limit_event":
                                print(_render_rate_limit_line(obj))
                            elif obj.get("type") == "result":
                                print(_render_result_line(obj))
                    else:
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                else:
                    if verdict == "denied":
                        break
                    if live_pid and not claims.pid_alive(live_pid):
                        break
                    if not live_pid:
                        break
                    time.sleep(0.25)
        return 0

    if args.json or not is_stream:
        with log_path.open(encoding="utf-8", errors="replace") as f:
            sys.stdout.write(f.read())
    else:
        _render_stream_jsonl(log_path)
    return 0


def cmd_tui(args) -> int:
    try:
        from .tui import main as tui_main
    except ImportError:
        print(
            "textual is not installed. Install it with:\n"
            "  pip install 'maestro-orchestrator[tui]'",
            file=sys.stderr,
        )
        return 2
    return tui_main(args)


def cmd_prune_logs(args) -> int:
    """[human] delete stale session logs per retention settings (--dry-run to preview)."""
    cfg = _cfg(args)
    if not args.key and not args.all:
        print("error: pass a ticket key or --all", file=sys.stderr)
        return 2
    keys = None if args.all else [args.key]
    result = ops.prune_all_session_logs(cfg, now=store.now_epoch(), dry_run=args.dry_run, keys=keys)
    _print(result)
    return 0


def cmd_backup(args) -> int:
    """Snapshot the irreplaceable state (events/tickets/inbox/config) to a tarball."""
    cfg = _cfg(args)
    if args.list:
        _print({"backup_dir": str(backup.resolve_backup_dir(cfg)),
                "backups": [str(p) for p in backup.list_backups(cfg)]})
        return 0
    path = backup.create_backup(cfg, store.now_epoch())
    _print({"created": str(path)})
    return 0


def cmd_local_backup(args) -> int:
    """[agent] AD-6: back up a mode="local" ticket's target dir before writing in place."""
    cfg = _cfg(args)
    archive = ops.local_write_backup(cfg, args.key, actor=args.actor)
    _print({"backed_up": archive})
    return 0


def cmd_install_commands(args) -> int:
    """GA-15: idempotently install the six per-phase reconcile command files —
    ``--repo <name>`` copies them into a configured repo's checkout,
    ``--user`` symlinks them into the user commands directory instead (for a
    repo the board doesn't own). Replaces DOGFOOD.md's old "vendor by hand"
    step."""
    cfg = _cfg(args)
    if bool(args.repo) == bool(args.user):
        print("error: exactly one of --repo <name> or --user is required", file=sys.stderr)
        return 2
    if args.user:
        _print(skills_install.install_user(cfg))
    else:
        _print(skills_install.install_repo(cfg, args.repo))
    return 0


def cmd_restore(args) -> int:
    """Restore a backup tarball into the home, then refold snapshots + dashboards."""
    cfg = _cfg(args)
    archive = Path(args.archive) if args.archive else None
    _print(backup.restore_backup(cfg, archive, force=args.force))
    return 0


def cmd_env(args) -> int:
    """Resolved config essentials — used by the reconcile skill to find the repo."""
    cfg = _cfg(args)
    key = getattr(args, "key", None)
    if key:
        name = repos_mod.bound_repo_name(cfg.home, key)
        if name and name not in cfg.repos:
            print(f"error: {key} is bound to repo '{name}' but no [repos.{name}] "
                  f"table is configured", file=sys.stderr)
            return 1
        binding = repos_mod.resolve(cfg, cfg.home, key)
        snap = snap_mod.load(cfg.home, key)
        _print({"repo": binding.name, "repo_path": binding.path, "slug": binding.slug,
                "base_branch": binding.base_branch, "branch_prefix": binding.branch_prefix,
                "mode": binding.mode,
                "reconcile_command": disp.resolve_reconcile_command(cfg, snap.phase)})
        return 0
    _print({"home": str(cfg.home), "repo_path": cfg.repo_path,
            "branch_prefix": cfg.branch_prefix, "reconcile_command": cfg.reconcile_command,
            "max_concurrency": cfg.max_concurrency, "max_impl_turns": cfg.max_impl_turns,
            "qa_standards_axis": cfg.qa_standards_axis, "providers": cfg.providers})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="maestro", description="Per-ticket reconciler orchestrator.")
    p.add_argument("--home", help="MAESTRO_HOME (default: $MAESTRO_HOME or ~/.maestro)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help=""):
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(func=fn)
        return sp

    add("init", cmd_init, "scaffold a maestro home + config")

    sp = add("create", cmd_create, "queue a new ticket (no args = interactive)")
    sp.add_argument("title", nargs="?", default=None,
                    help="ticket title; omit for guided interactive flow")
    sp.add_argument("--key"); sp.add_argument("--tier", type=int, default=1)
    sp.add_argument("--priority", type=int, default=3); sp.add_argument("--intent")
    sp.add_argument("--kind", default=None, help="ticket kind (e.g. research, implementation)")
    sp.add_argument("--model", default=None, help="model override for this ticket's reconciler")
    sp.add_argument("--effort", default=None, help="effort override (low/medium/high/xhigh/max)")
    sp.add_argument("--notes", default=None, help="text for the ## Notes section")
    sp.add_argument("--depends-on", dest="depends_on", nargs="+", default=None,
                    metavar="KEY", help="ticket keys this ticket depends on")
    sp.add_argument("--repo", default=None,
                    help="[repos.<name>] binding for this ticket's reconciler")
    sp.add_argument("--no-nudge", action="store_true", dest="no_nudge",
                    help="skip in-process dispatch nudge after queuing")

    sp = add("ans", cmd_ans, "answer a ticket's open question")
    sp.add_argument("key"); sp.add_argument("text"); sp.add_argument("--qid")
    sp.add_argument("--no-nudge", action="store_true", dest="no_nudge",
                    help="skip in-process dispatch nudge after answering")

    sp = add("approve", cmd_approve,
             "[human] clear a tier-2 ticket's implementing gate (due next sweep)")
    sp.add_argument("key")
    sp.add_argument("--no-nudge", action="store_true", dest="no_nudge",
                    help="skip in-process dispatch nudge after approving")

    sp = add("answer", cmd_answer, "interactive walkthrough of all open questions")
    sp.add_argument("key", nargs="?", default=None, help="scope to one ticket (default: all)")

    sp = add("cmd", cmd_cmd, "send an arbitrary command (retry/discard/...)")
    sp.add_argument("key"); sp.add_argument("command"); sp.add_argument("rest", nargs="*")
    sp.add_argument("--no-nudge", action="store_true", dest="no_nudge",
                    help="skip in-process dispatch nudge after sending command")

    add("status", cmd_status, "summary of all tickets")
    sp = add("show", cmd_show, "snapshot + events for one ticket")
    sp.add_argument("key"); sp.add_argument("--tail", type=int, default=12)
    sp = add("logs", cmd_logs, "view captured session logs for a ticket")
    sp.add_argument("key", nargs="?", default=None)
    sp.add_argument("--key", dest="key_flag", default=None, help="ticket key (alternative to positional)")
    sp.add_argument("--list", action="store_true", help="list sessions newest-first")
    sp.add_argument("--session", help="select a specific session_id")
    sp.add_argument("--follow", action="store_true", help="tail the live session log")
    sp.add_argument("--json", action="store_true", help="emit raw stream-jsonl lines")
    add("doctor", cmd_doctor, "fleet health (heartbeat, dead-letters, spawn-rate runaway)")

    sp = add("why", cmd_why, "recent dispatcher decisions for one key (derived/dispatch.jsonl tail)")
    sp.add_argument("key")
    sp.add_argument("--tail", type=int, default=20, help="how many recent sweep decisions to show")

    sp = add("ratelimit", cmd_ratelimit, "show/clear the fleet-wide rate-limit pause")
    sp.add_argument("--clear", action="store_true", help="remove any active pause")

    sp = add("dispatch", cmd_dispatch, "one dispatcher sweep (launchd calls this)")
    sp.add_argument("--dry-run", action="store_true",
                    help="preview only: report would_mint/would_spawn, no writes "
                         "except regenerated dashboards (see what WOULD happen, no "
                         "sessions launched, nothing minted, ledger/attempts untouched)")
    sp.add_argument("--model", default=None, help="override reconcile_model from config")
    add("project", cmd_project, "regenerate dashboards")
    sp = add("env", cmd_env, "resolved config (home, repo_path, ...)")
    sp.add_argument("--key", default=None,
                    help="resolve one ticket's repo binding instead of the bare config")

    sp = add("schedule", cmd_schedule,
             "list/add/edit/rm/enable/disable config-declared [[scheduled]] tasks")
    sp.add_argument("action", choices=["list", "add", "edit", "rm", "enable", "disable"],
                    nargs="?", default="list")
    sp.add_argument("name", nargs="?", default=None,
                    help="task name (required for add/edit/rm/enable/disable)")
    sp.add_argument("--prompt", default=None, help="add/edit: the minted ticket's intent")
    sp.add_argument("--every", default=None,
                    help="add/edit: interval cadence, e.g. 30m/6h/24h/seconds "
                         "(exactly one of --every/--cron)")
    sp.add_argument("--cron", default=None,
                    help="add/edit: 5-field cron expression, e.g. '0 9 * * 1' "
                         "(exactly one of --every/--cron)")
    sp.add_argument("--tz", default=None,
                    help="add/edit: IANA timezone for --cron (default: UTC)")
    sp.add_argument("--kind", default=None, choices=["implementation", "research"])
    sp.add_argument("--approval-tier", dest="approval_tier", type=int, default=None)
    sp.add_argument("--priority", type=int, default=None)
    sp.add_argument("--prefix", default=None, help="minted keys become PREFIX-1, PREFIX-2, …")
    sp.add_argument("--title", default=None)
    sp.add_argument("--repo", default=None, help="must match a [repos.<name>] table")
    sp.add_argument("--model", default=None)
    sp.add_argument("--effort", default=None)
    sp.add_argument("--notes", default=None)
    sp.add_argument("--depends-on", dest="depends_on", nargs="+", default=None, metavar="KEY")
    sp.add_argument("--enabled", dest="enabled", action="store_true", default=None)
    sp.add_argument("--disabled", dest="enabled", action="store_false", default=None)

    sp = add("prune-logs", cmd_prune_logs, "[human] delete stale session logs per retention settings")
    sp.add_argument("key", nargs="?", default=None, help="prune only this ticket's logs")
    sp.add_argument("--all", action="store_true", help="prune every key reachable under agent-logs/")
    sp.add_argument("--dry-run", action="store_true", help="report what would be pruned; delete nothing")

    sp = add("backup", cmd_backup, "snapshot events/tickets/inbox/config to a tarball")
    sp.add_argument("--list", action="store_true",
                    help="list existing backups instead of creating one")
    sp = add("local-backup", cmd_local_backup,
             "[agent] back up a mode=\"local\" ticket's target dir before writing in place")
    sp.add_argument("key"); sp.add_argument("--actor", default="reconciler")
    sp = add("install-commands", cmd_install_commands,
             "install the six per-phase maestro-reconcile-*.md commands into a repo or user dir")
    sp.add_argument("--repo", default=None,
                    help="[repos.<name>] (or 'default' for the legacy repo_path) to copy into")
    sp.add_argument("--user", action="store_true",
                    help="symlink into the user commands directory instead of a repo checkout")

    sp = add("restore", cmd_restore, "restore a backup tarball into the home")
    sp.add_argument("archive", nargs="?", default=None,
                    help="tarball path to restore (default: latest)")
    sp.add_argument("--force", action="store_true",
                    help="overwrite a non-empty events/ or tickets/")

    sp = add("fleet", cmd_fleet,
             "manage the launchd dispatcher (up/down/status) and the pause kill switch")
    sp.description = (
        "pause/resume stop the DISPATCHER from minting or spawning NEW reconcilers; "
        "they do not touch sessions already running (see a T-13 watchdog for that) "
        "and do not unload the launchd agent (use 'fleet down' for that)."
    )
    sp.add_argument("action", choices=["up", "down", "status", "pause", "resume"])
    sp.add_argument("--interval", type=int, default=300, help="dispatch cadence (seconds)")
    sp.add_argument("--for", dest="for_", default=None,
                    help="pause [action=pause] for a duration (30m/6h/24h/7d/seconds)")
    sp.add_argument("--until", default=None,
                    help="pause [action=pause] until a bare epoch or ISO-8601 timestamp")
    sp.add_argument("--reason", default=None, help="pause [action=pause] reason")

    sp = add("snapshot", cmd_snapshot, "[agent] folded snapshot"); sp.add_argument("key")
    sp = add("events", cmd_events, "[agent] event log"); sp.add_argument("key"); sp.add_argument("--since", type=int, default=0)

    sp = add("append", cmd_append, "[agent] append a raw event")
    sp.add_argument("key"); sp.add_argument("--type", required=True)
    sp.add_argument("--payload"); sp.add_argument("--step-id"); sp.add_argument("--expect", type=int)
    sp.add_argument("--actor", default="reconciler")

    sp = add("set-phase", cmd_set_phase, "[agent] advance phase")
    sp.add_argument("key"); sp.add_argument("phase", choices=[p.value for p in Phase])
    sp.add_argument("--reason"); sp.add_argument("--requeue", type=int); sp.add_argument("--actor", default="reconciler")
    sp.add_argument("--force", action="store_true",
                     help="override the AC-verification gate on awaiting-ci (records --actor as forced_by)")

    sp = add("ask", cmd_ask, "[agent] ask the human, go to awaiting-human")
    sp.add_argument("key")
    sp.add_argument("text", nargs="?", default=None,
                     help="single-question text (omit when using --question)")
    sp.add_argument("--qid")
    sp.add_argument("--question", dest="questions", action="append", nargs=3,
                     metavar=("TEXT", "RECOMMENDED", "QID"),
                     help="repeatable: post one question of a multi-question frontier round "
                          "in this single call -- pass '' for RECOMMENDED when that question "
                          "has no recommended answer, and '' for QID to auto-derive it (only "
                          "give an explicit QID when a later step routes on its prefix, e.g. "
                          "research-approval-<key>). Mutually exclusive with TEXT/--qid.")
    sp.add_argument("--actor", default="reconciler")

    sp = add("fold-inbox", cmd_fold_inbox, "[agent] fold pending human commands into events")
    sp.add_argument("key")
    sp = add("inbox-ack", cmd_inbox_ack, "[agent] advance inbox cursor (after phase advance)")
    sp.add_argument("key")
    sp = add("observe-spec", cmd_observe_spec, "[agent] record current spec hash"); sp.add_argument("key"); sp.add_argument("--actor", default="reconciler")
    sp = add("requeue", cmd_requeue, "[agent] schedule a re-wake")
    sp.add_argument("key"); sp.add_argument("seconds", type=int); sp.add_argument("--actor", default="reconciler")
    sp = add("fail", cmd_fail, "[agent] record failure (backoff or dead-letter)")
    sp.add_argument("key"); sp.add_argument("error"); sp.add_argument("--actor", default="reconciler")
    sp = add("impl-turn", cmd_impl_turn,
             "[agent] record one implementing turn; parks the ticket past max_impl_turns")
    sp.add_argument("key"); sp.add_argument("--role", default="implementer")
    sp.add_argument("--actor", default="reconciler")
    sp = add("verify-ac", cmd_verify_ac, "[agent] attest AC #n with structured evidence (content-hash keyed)")
    sp.add_argument("key"); sp.add_argument("--ac", type=int, required=True, dest="ac")
    sp.add_argument("--what", required=True, help="what was run (e.g. a command or test)")
    sp.add_argument("--where", required=True, help="where it ran (file:line or test name)")
    sp.add_argument("--result", required=True, help="the observed result (e.g. PASSED, output excerpt)")
    sp.add_argument("--actor", default="reconciler")

    sp = add("qa-verdict", cmd_qa_verdict,
             "[agent] record an independent QA pass/fail verdict for AC #n (content-hash keyed)")
    sp.add_argument("key"); sp.add_argument("--ac", type=int, required=True, dest="ac")
    sp.add_argument("--verdict", required=True, choices=sorted(ops.QA_VERDICTS))
    sp.add_argument("--evidence", required=True); sp.add_argument("--actor", default="reconciler-qa")
    sp.add_argument("--axis", default="spec", choices=sorted(ops.QA_AXES),
                    help="which QA axis this verdict belongs to (default: spec, gates "
                         "awaiting-ci; standards is advisory-only, T-23)")

    sp = add("finalize", cmd_finalize, "[agent] tombstone a finished ticket"); sp.add_argument("key"); sp.add_argument("--actor", default="reconciler")
    sp = add("compact", cmd_compact, "fold pre-snapshot events into archive"); sp.add_argument("key")
    sp = add("archive-done", cmd_archive_done, "[maintenance] move DONE tickets out of the active scan")
    sp.add_argument("--after", type=float, default=0, help="grace period in seconds since the ticket's last event")
    sp = add("release", cmd_release, "[agent] drop this ticket's claim on exit"); sp.add_argument("key")

    sp = add("claims", cmd_claims, "list claim files with verified identity (key/pid/age/verdict)")
    sp.add_argument("--purge", action="store_true", help="release denied and over-age claims")

    sp = add("check-conflicts", cmd_check_conflicts,
             "[agent] route to implementing for auto-resolution if PR is CONFLICTING (idempotent)")
    sp.add_argument("key")
    sp.add_argument("pr_number", type=int)
    sp.add_argument("state", help="mergeable state from gh (CONFLICTING|MERGEABLE|UNKNOWN)")
    sp.add_argument("--actor", default="reconciler")

    sp = add("check-merged", cmd_check_merged,
             "[agent] finalize if PR state is MERGED — callable from any phase (idempotent)")
    sp.add_argument("key")
    sp.add_argument("state", help="GitHub PR state (MERGED|OPEN|CLOSED)")
    sp.add_argument("--actor", default="reconciler")

    sp = add("fold-steps", cmd_fold_steps, "[agent] fold stream steps into IMPL_STEP events")
    sp.add_argument("key")
    sp.add_argument("--log", default=None, help="path to a specific stream.jsonl (default: current claim log)")

    add("tui", cmd_tui, "launch the interactive TUI (requires [tui] extra)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except store.MaestroError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
