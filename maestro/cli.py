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
from pathlib import Path

from . import backup, claims, event_log, fleet, inbox, ops, projection, snapshot as snap_mod, steplog, store
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


def _web_tools_extra_args(cfg: Config) -> list[str]:
    """--allowedTools flag granting spawned reconcilers WebSearch/WebFetch, if enabled."""
    return ["--allowedTools", "WebSearch,WebFetch"] if cfg.reconcile_web_tools else []


def _nudge(cfg: Config) -> None:
    """In-process dispatch sweep after a human-input verb (ans/cmd/create).

    Spawns are detached Popen so this returns quickly. The existing per-key
    claim dedup prevents double-spawning if a reconciler is already live.
    """
    sessions = ClaudeCliSessions(
        cfg.home, model=cfg.reconcile_model,
        permission_mode=cfg.permission_mode,
        extra_args=_web_tools_extra_args(cfg),
        capture_session_logs=cfg.capture_session_logs,
        session_log_format=cfg.session_log_format,
    )
    disp.dispatch(cfg, sessions, now=store.now_epoch())


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

    a = {"approval_tier": args.tier, "priority": args.priority}
    if args.intent:
        a["intent"] = args.intent
    if getattr(args, "kind", None):
        a["kind"] = args.kind
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
    hb = store.read_json(cfg.home / "derived" / ".heartbeat.json", {})
    age = None
    if hb.get("epoch"):
        age = round(store.now_epoch() - hb["epoch"])
    dead = list((cfg.home / "tickets" / "_deadletter").glob("*.md")) \
        if (cfg.home / "tickets" / "_deadletter").exists() else []
    _print({"heartbeat": hb, "heartbeat_age_s": age,
            "dead_letters": [p.stem for p in dead],
            "stale": age is not None and age > 1800})
    return 0


# --- dispatcher / projection (launchd) --------------------------------------
def cmd_dispatch(args) -> int:
    cfg = _cfg(args)
    if args.dry_run:
        sessions = DryRunSessions()
    else:
        sessions = ClaudeCliSessions(
            cfg.home, model=args.model or cfg.reconcile_model,
            permission_mode=cfg.permission_mode,
            extra_args=_web_tools_extra_args(cfg),
            session_log_format=cfg.session_log_format)
    report = disp.dispatch(cfg, sessions, now=store.now_epoch())
    projection.write(cfg.home)
    out = {"minted": report.minted,
           "spawned" if not args.dry_run else "would_spawn": report.spawned,
           "claimed": report.claimed, "capacity_skipped": report.capacity_skipped,
           "throttled": report.throttled,
           "active_sessions": report.active_sessions,
           "scheduled_fired": report.scheduled_fired,
           "reaped": report.reaped,
           "due": [{"key": k, "reason": r} for k, r in report.due]}
    _print(out)
    return 0


def cmd_schedule(args) -> int:
    cfg = _cfg(args)
    rows = disp.schedule_status(cfg, store.now_epoch())
    _print({"scheduled": rows})
    return 0


def cmd_project(args) -> int:
    cfg = _cfg(args)
    written = projection.write(cfg.home)
    _print({"wrote": written})
    return 0


def cmd_fleet(args) -> int:
    cfg = _cfg(args)
    if args.action == "up":
        _print(fleet.up(cfg.home, interval=args.interval))
    elif args.action == "down":
        _print(fleet.down(cfg.home))
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


def cmd_append(args) -> int:
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
                       actor=args.actor, requeue_in=args.requeue)
    _print(ev or {"noop": "phase already set"})
    return 0


def cmd_ask(args) -> int:
    qid = ops.ask(_cfg(args), args.key, args.text, qid=args.qid, actor=args.actor)
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


def cmd_check_conflicts(args) -> int:
    """Route a CONFLICTING PR back to implementing for auto-resolution (idempotent)."""
    if args.state != "CONFLICTING":
        _print({"conflict": False, "reason": f"mergeable={args.state}"})
        return 0
    routed = ops.route_conflict(_cfg(args), args.key, args.pr_number, actor=args.actor)
    _print({"conflict": True, "routed_to": "implementing", "moved": routed})
    return 0


def cmd_verify_ac(args) -> int:
    """[agent] attest AC #n with evidence — content-hash keyed, idempotent."""
    h = ops.verify_ac(_cfg(args), args.key, args.ac, args.evidence, actor=args.actor)
    _print({"verified_ac_hash": h})
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


def cmd_release(args) -> int:
    """A reconciler calls this on exit to drop its claim (best-effort)."""
    claims.release(_cfg(args).home, args.key)
    _print({"released": args.key})
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
            elif obj.get("type") == "result":
                # Flush collected assistant messages in order, then show result
                for _, msg_obj in seen_msg_ids.items():
                    _print_assistant_message(msg_obj)
                seen_msg_ids.clear()
                sub = obj.get("subtype", "")
                dur = obj.get("duration_ms")
                suffix = f" ({dur}ms)" if dur else ""
                print(f"[result:{sub}]{suffix}")
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
    sessions = list_sessions(cfg.home, key)

    if args.list:
        _print(sessions)
        return 0

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
        # Tail the file; stop when the session process is gone
        claim = claims.read_claim(cfg.home, key)
        live_pid = claim.get("pid") if claim else None
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
                            elif obj.get("type") == "result":
                                sub = obj.get("subtype", "")
                                print(f"[result:{sub}]")
                    else:
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                else:
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


def cmd_restore(args) -> int:
    """Restore a backup tarball into the home, then refold snapshots + dashboards."""
    cfg = _cfg(args)
    archive = Path(args.archive) if args.archive else None
    _print(backup.restore_backup(cfg, archive, force=args.force))
    return 0


def cmd_env(args) -> int:
    """Resolved config essentials — used by the reconcile skill to find the repo."""
    cfg = _cfg(args)
    _print({"home": str(cfg.home), "repo_path": cfg.repo_path,
            "branch_prefix": cfg.branch_prefix, "reconcile_command": cfg.reconcile_command,
            "max_concurrency": cfg.max_concurrency, "max_impl_turns": cfg.max_impl_turns,
            "providers": cfg.providers})
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
    sp.add_argument("--no-nudge", action="store_true", dest="no_nudge",
                    help="skip in-process dispatch nudge after queuing")

    sp = add("ans", cmd_ans, "answer a ticket's open question")
    sp.add_argument("key"); sp.add_argument("text"); sp.add_argument("--qid")
    sp.add_argument("--no-nudge", action="store_true", dest="no_nudge",
                    help="skip in-process dispatch nudge after answering")

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
    add("doctor", cmd_doctor, "fleet health (heartbeat, dead-letters)")

    sp = add("dispatch", cmd_dispatch, "one dispatcher sweep (launchd calls this)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--model", default=None, help="override reconcile_model from config")
    add("project", cmd_project, "regenerate dashboards")
    add("env", cmd_env, "resolved config (home, repo_path, ...)")

    sp = add("schedule", cmd_schedule, "list config-declared scheduled tasks (name/cadence/last_fired/next_due)")
    sp.add_argument("action", choices=["list"], nargs="?", default="list")

    sp = add("backup", cmd_backup, "snapshot events/tickets/inbox/config to a tarball")
    sp.add_argument("--list", action="store_true",
                    help="list existing backups instead of creating one")
    sp = add("restore", cmd_restore, "restore a backup tarball into the home")
    sp.add_argument("archive", nargs="?", default=None,
                    help="tarball path to restore (default: latest)")
    sp.add_argument("--force", action="store_true",
                    help="overwrite a non-empty events/ or tickets/")

    sp = add("fleet", cmd_fleet, "manage the launchd dispatcher (up/down/status)")
    sp.add_argument("action", choices=["up", "down", "status"])
    sp.add_argument("--interval", type=int, default=300, help="dispatch cadence (seconds)")

    sp = add("snapshot", cmd_snapshot, "[agent] folded snapshot"); sp.add_argument("key")
    sp = add("events", cmd_events, "[agent] event log"); sp.add_argument("key"); sp.add_argument("--since", type=int, default=0)

    sp = add("append", cmd_append, "[agent] append a raw event")
    sp.add_argument("key"); sp.add_argument("--type", required=True)
    sp.add_argument("--payload"); sp.add_argument("--step-id"); sp.add_argument("--expect", type=int)
    sp.add_argument("--actor", default="reconciler")

    sp = add("set-phase", cmd_set_phase, "[agent] advance phase")
    sp.add_argument("key"); sp.add_argument("phase", choices=[p.value for p in Phase])
    sp.add_argument("--reason"); sp.add_argument("--requeue", type=int); sp.add_argument("--actor", default="reconciler")

    sp = add("ask", cmd_ask, "[agent] ask the human, go to awaiting-human")
    sp.add_argument("key"); sp.add_argument("text"); sp.add_argument("--qid"); sp.add_argument("--actor", default="reconciler")

    sp = add("fold-inbox", cmd_fold_inbox, "[agent] fold pending human commands into events")
    sp.add_argument("key")
    sp = add("inbox-ack", cmd_inbox_ack, "[agent] advance inbox cursor (after phase advance)")
    sp.add_argument("key")
    sp = add("observe-spec", cmd_observe_spec, "[agent] record current spec hash"); sp.add_argument("key"); sp.add_argument("--actor", default="reconciler")
    sp = add("requeue", cmd_requeue, "[agent] schedule a re-wake")
    sp.add_argument("key"); sp.add_argument("seconds", type=int); sp.add_argument("--actor", default="reconciler")
    sp = add("fail", cmd_fail, "[agent] record failure (backoff or dead-letter)")
    sp.add_argument("key"); sp.add_argument("error"); sp.add_argument("--actor", default="reconciler")
    sp = add("verify-ac", cmd_verify_ac, "[agent] attest AC #n with evidence (content-hash keyed)")
    sp.add_argument("key"); sp.add_argument("--ac", type=int, required=True, dest="ac")
    sp.add_argument("--evidence", required=True); sp.add_argument("--actor", default="reconciler")

    sp = add("finalize", cmd_finalize, "[agent] tombstone a finished ticket"); sp.add_argument("key"); sp.add_argument("--actor", default="reconciler")
    sp = add("compact", cmd_compact, "fold pre-snapshot events into archive"); sp.add_argument("key")
    sp = add("release", cmd_release, "[agent] drop this ticket's claim on exit"); sp.add_argument("key")

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
