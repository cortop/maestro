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
from pathlib import Path

from . import claims, event_log, inbox, ops, projection, snapshot as snap_mod, store
from . import dispatcher as disp
from .config import Config, DEFAULT_CONFIG_TOML, config_path, load
from .sessions import ClaudeCliSessions, DryRunSessions
from .statemachine import Phase

HOME_DIRS = ["events", "inbox", "tickets", "worktrees",
             "derived/snapshots", "derived/cursors", "derived/claims", "agent-logs"]


def _cfg(args) -> Config:
    return load(getattr(args, "home", None))


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str) if not isinstance(obj, str) else obj)


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


def cmd_create(args) -> int:
    cfg = _cfg(args)
    a = {"approval_tier": args.tier, "priority": args.priority}
    if args.intent:
        a["intent"] = args.intent
    inbox.append_new(cfg.home, args.title, key=args.key, args=a)
    _print(f"queued create: {args.key or '(auto-key)'} — {args.title}")
    return 0


def cmd_ans(args) -> int:
    cfg = _cfg(args)
    a = {"text": args.text}
    if args.qid:
        a["qid"] = args.qid
    inbox.append_command(cfg.home, args.key, "ans", a)
    _print(f"answer queued for {args.key}")
    return 0


def cmd_cmd(args) -> int:
    cfg = _cfg(args)
    inbox.append_command(cfg.home, args.key, args.command,
                         {"text": " ".join(args.rest)} if args.rest else {})
    _print(f"command '{args.command}' queued for {args.key}")
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
            permission_mode=cfg.permission_mode)
    report = disp.dispatch(cfg, sessions, now=store.now_epoch())
    projection.write(cfg.home)
    out = {"minted": report.minted,
           "spawned" if not args.dry_run else "would_spawn": report.spawned,
           "claimed": report.claimed, "capacity_skipped": report.capacity_skipped,
           "active_sessions": report.active_sessions,
           "due": [{"key": k, "reason": r} for k, r in report.due]}
    _print(out)
    return 0


def cmd_project(args) -> int:
    cfg = _cfg(args)
    written = projection.write(cfg.home)
    _print({"wrote": written})
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


def cmd_finalize(args) -> int:
    cfg = _cfg(args)
    ops.finalize(cfg, args.key, actor=args.actor)
    _print({"finalized": args.key})
    return 0


def cmd_release(args) -> int:
    """A reconciler calls this on exit to drop its claim (best-effort)."""
    claims.release(_cfg(args).home, args.key)
    _print({"released": args.key})
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

    sp = add("create", cmd_create, "queue a new ticket")
    sp.add_argument("title")
    sp.add_argument("--key"); sp.add_argument("--tier", type=int, default=1)
    sp.add_argument("--priority", type=int, default=3); sp.add_argument("--intent")

    sp = add("ans", cmd_ans, "answer a ticket's open question")
    sp.add_argument("key"); sp.add_argument("text"); sp.add_argument("--qid")

    sp = add("cmd", cmd_cmd, "send an arbitrary command (retry/discard/...)")
    sp.add_argument("key"); sp.add_argument("command"); sp.add_argument("rest", nargs="*")

    add("status", cmd_status, "summary of all tickets")
    sp = add("show", cmd_show, "snapshot + events for one ticket")
    sp.add_argument("key"); sp.add_argument("--tail", type=int, default=12)
    add("doctor", cmd_doctor, "fleet health (heartbeat, dead-letters)")

    sp = add("dispatch", cmd_dispatch, "one dispatcher sweep (launchd calls this)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--model", default=None, help="override reconcile_model from config")
    add("project", cmd_project, "regenerate dashboards")
    add("env", cmd_env, "resolved config (home, repo_path, ...)")

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
    sp = add("finalize", cmd_finalize, "[agent] tombstone a finished ticket"); sp.add_argument("key"); sp.add_argument("--actor", default="reconciler")
    sp = add("release", cmd_release, "[agent] drop this ticket's claim on exit"); sp.add_argument("key")
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
