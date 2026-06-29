"""High-level reconciler operations — the verbs an agent uses, each correct by
construction. Every verb: appends event(s) with a deterministic step-id, then
refreshes the snapshot. Idempotent under crash-and-respawn.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from . import events as E
from . import event_log, inbox, snapshot as snap_mod, store
from .config import Config
from .dispatcher import spec_hash_on_disk
from .idempotency import content_hash, step_id
from .statemachine import Phase, can_transition

ANSWER_COMMANDS = {"ans", "answer", "approve", "yes", "ok", "no", "reject", "discard", "retry"}


def _append(cfg: Config, key: str, type: str, payload: dict, *, actor: str,
            sid: str | None, expect: int | None = None) -> dict | None:
    ev = event_log.append(cfg.home, key, type, payload, actor=actor,
                          step_id=sid, expected_last_seq=expect)
    snap_mod.rebuild(cfg.home, key)
    return ev


def set_phase(cfg: Config, key: str, phase: Phase, *, reason: str = "", actor: str = "reconciler",
              requeue_in: int | None = None, expect: int | None = None) -> dict | None:
    snap = snap_mod.load(cfg.home, key)
    src = Phase(snap.phase)
    if src != phase and not can_transition(src, phase):
        # Not fatal — log it, but the engine trusts the agent's judgment.
        _append(cfg, key, E.NOTE, {"text": f"unusual transition {src.value}->{phase.value}"},
                actor=actor, sid=step_id(key, snap.phase, snap.observed_seq, f"note-transition-{phase.value}"))
        snap = snap_mod.load(cfg.home, key)
    sid = step_id(key, snap.phase, snap.observed_seq, f"phase:{phase.value}")
    ev = _append(cfg, key, E.PHASE_CHANGED, {"phase": phase.value, "reason": reason},
                 actor=actor, sid=sid, expect=expect)
    if requeue_in is not None:
        requeue(cfg, key, requeue_in, actor=actor)
    return ev


def ask(cfg: Config, key: str, text: str, *, qid: str | None = None, actor: str = "reconciler") -> str:
    qid = qid or content_hash(text)
    _append(cfg, key, E.QUESTION_ASKED, {"qid": qid, "text": text},
            actor=actor, sid=f"ask-{key}-{qid}")
    set_phase(cfg, key, Phase.AWAITING_HUMAN, reason="asked human", actor=actor)
    return qid


def ask_conflict(cfg: Config, key: str, pr_number: int, *, actor: str = "reconciler") -> bool:
    """Ask the human to resolve a PR merge conflict (idempotent — skips if already open)."""
    snap = snap_mod.load(cfg.home, key)
    qid = f"conflict-{key}-{pr_number}"
    if qid in snap.open_questions:
        return False
    text = (f"PR #{pr_number} has a merge conflict with the base branch. "
            f"Please rebase, resolve the conflicts, and push again.")
    ask(cfg, key, text, qid=qid, actor=actor)
    return True


def check_merged(cfg: Config, key: str, pr_state: str, *, actor: str = "reconciler") -> bool:
    """Finalize if the PR is merged — callable from any phase (idempotent).

    Records PrUpdated(merged=True) then Finalized. Returns True if finalized,
    False if the state isn't MERGED or the ticket is already done.
    """
    if pr_state.upper() != "MERGED":
        return False
    snap = snap_mod.load(cfg.home, key)
    if snap.phase == Phase.DONE.value:
        return False
    _append(cfg, key, E.PR_UPDATED, {"merged": True},
            actor=actor, sid=f"pr-merged-{key}")
    finalize(cfg, key, actor=actor)
    return True


def observe_spec(cfg: Config, key: str, *, actor: str = "reconciler") -> str | None:
    h = spec_hash_on_disk(cfg.home, key)
    if h is None:
        return None
    _append(cfg, key, E.SPEC_OBSERVED, {"spec_hash": h}, actor=actor, sid=f"spec-{key}-{h}")
    return h


def requeue(cfg: Config, key: str, seconds: int, *, actor: str = "reconciler") -> None:
    at = store.now_epoch() + max(0, seconds)
    snap = snap_mod.load(cfg.home, key)
    _append(cfg, key, E.REQUEUE_SCHEDULED, {"at": at}, actor=actor,
            sid=step_id(key, snap.phase, snap.observed_seq, f"requeue:{int(at)}"))


def fail(cfg: Config, key: str, error: str, *, actor: str = "reconciler") -> str:
    """Record a failure; back off, or dead-letter if over the threshold."""
    snap = snap_mod.load(cfg.home, key)
    _append(cfg, key, E.FAILED, {"error": error}, actor=actor,
            sid=step_id(key, snap.phase, snap.observed_seq, "fail"))
    snap = snap_mod.load(cfg.home, key)
    if snap.failure_count >= cfg.max_failures:
        _append(cfg, key, E.STALLED, {"reason": f"{snap.failure_count} failures: {error}"},
                actor=actor, sid=f"deadletter-{key}-{snap.observed_seq}")
        _write_deadletter(cfg, key, error)
        return "dead-letter"
    delay = min(cfg.backoff_cap, cfg.backoff_base * (2 ** snap.failure_count))
    delay = int(delay * random.uniform(0.7, 1.3))  # jitter: avoid thundering herd
    requeue(cfg, key, delay, actor=actor)
    return f"backoff:{delay}s"


def _write_deadletter(cfg: Config, key: str, error: str) -> None:
    tail = event_log.read(cfg.home, key)[-8:]
    body = [f"# {key} — dead-lettered", "", f"Last error: {error}", "",
            "## Recent events"]
    for e in tail:
        body.append(f"- seq {e['seq']} {e['type']} ({e['ts']})")
    body += ["", "Revive with: `maestro cmd %s retry`  ·  Drop with: `maestro cmd %s discard`"
             % (key, key)]
    store.atomic_write(store.deadletter_path(cfg.home, key), "\n".join(body) + "\n")


def fold_inbox(cfg: Config, key: str) -> list[dict]:
    """Fold pending human commands into events (idempotent), WITHOUT acking.
    The reconciler acks only after it has advanced the phase, so a crash mid-step
    re-reads the same commands next sweep.
    """
    base = len(store.read_jsonl(store.inbox_path(cfg.home, key))) - len(inbox.pending(cfg.home, key))
    pend = inbox.pending(cfg.home, key)
    snap = snap_mod.load(cfg.home, key)
    open_qids = list(snap.open_questions.keys())
    for i, cmd in enumerate(pend):
        idx = base + i
        command = cmd.get("command", "")
        _append(cfg, key, E.COMMAND_RECEIVED, {"command": command, "args": cmd.get("args", {})},
                actor="human", sid=f"cmd-{key}-{idx}")
        if command in ANSWER_COMMANDS:
            target = cmd.get("args", {}).get("qid")
            qids = [target] if target else open_qids
            answer = cmd.get("args", {}).get("text", command)
            for qid in qids:
                _append(cfg, key, E.QUESTION_ANSWERED, {"qid": qid, "answer": answer},
                        actor="human", sid=f"ans-{key}-{idx}-{qid}")
    return pend


def finalize(cfg: Config, key: str, *, actor: str = "reconciler") -> None:
    _append(cfg, key, E.FINALIZED, {}, actor=actor, sid=f"finalize-{key}")


def prune_session_logs(cfg: Config, key: str) -> int:
    """Delete stale session log files for *key* per retention settings.

    Never removes the log belonging to a currently-live reconciler (pid alive).
    Returns the number of files deleted.
    """
    from . import claims
    from .sessions import list_sessions

    retention_days = cfg.session_log_retention_days
    max_per_ticket = cfg.session_log_max_per_ticket
    if retention_days is None and max_per_ticket is None:
        return 0

    all_files = list_sessions(cfg.home, key)
    if not all_files:
        return 0

    # Paths belonging to the live session (if any) are off-limits.
    live_paths: set[str] = set()
    claim = claims.read_claim(cfg.home, key)
    if claim and claims.pid_alive(claim.get("pid")):
        lp = claim.get("log_path")
        if lp:
            live_paths.add(lp)
            stem = lp.removesuffix(".stream.jsonl").removesuffix(".log")
            live_paths.add(stem + ".log")
            live_paths.add(stem + ".stream.jsonl")

    # Group files by session_id so .log and .stream.jsonl for the same session
    # are treated as one unit for the "max" limit.
    by_id: dict[str, list[dict]] = {}
    for f in all_files:
        by_id.setdefault(f["session_id"], []).append(f)

    # Sort session ids: newest epoch first.
    sorted_ids = sorted(
        by_id,
        key=lambda sid: max(f["epoch"] for f in by_id[sid]),
        reverse=True,
    )

    to_delete: set[str] = set()
    now = store.now_epoch()

    if max_per_ticket is not None:
        kept = 0
        for sid in sorted_ids:
            files = by_id[sid]
            if any(f["path"] in live_paths for f in files):
                continue  # live sessions don't count toward the limit
            if kept < max_per_ticket:
                kept += 1
            else:
                for f in files:
                    to_delete.add(f["path"])

    if retention_days is not None:
        cutoff = now - retention_days * 86400
        for sid in sorted_ids:
            files = by_id[sid]
            if any(f["path"] in live_paths for f in files):
                continue
            if max(f["epoch"] for f in files) < cutoff:
                for f in files:
                    to_delete.add(f["path"])

    pruned = 0
    for path_str in to_delete:
        try:
            Path(path_str).unlink(missing_ok=True)
            pruned += 1
        except OSError:
            pass
    return pruned


def compact(cfg: Config, key: str) -> dict:
    """Move events older than the snapshot into the archive.

    Under the active-log lock:
    - Reads snapshot to learn observed_seq (the high-water mark of the last fold).
    - Events with seq <= observed_seq are "pre-snapshot" and move to the archive.
    - Events with seq > observed_seq stay in the active log.
    - Archive grows monotonically; step_ids from archived events are still visible
      to _scan_tail so idempotency is preserved across compactions.
    """
    snap = snap_mod.load(cfg.home, key)
    cutoff = snap.observed_seq
    active_path = store.events_path(cfg.home, key)
    archive_path = store.events_archive_path(cfg.home, key)

    with store.file_lock(active_path):
        active_events = store.read_jsonl(active_path)
        pre = [e for e in active_events if isinstance(e.get("seq"), int) and e["seq"] <= cutoff]
        post = [e for e in active_events if not (isinstance(e.get("seq"), int) and e["seq"] <= cutoff)]

        if not pre:
            return {"archived": 0, "remaining": len(post), "cutoff_seq": cutoff}

        # Avoid double-archiving: only append events with seq > last archived seq.
        archived_events = store.read_jsonl(archive_path)
        last_archived_seq = max((e["seq"] for e in archived_events if isinstance(e.get("seq"), int)), default=0)
        to_archive = [e for e in pre if e["seq"] > last_archived_seq]

        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with archive_path.open("a", encoding="utf-8") as f:
            for ev in to_archive:
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")

        # Rewrite active log with only post-snapshot events.
        tmp = active_path.parent / f".{active_path.name}.compact.tmp"
        with tmp.open("w", encoding="utf-8") as f:
            for ev in post:
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        tmp.replace(active_path)

    pruned_logs = prune_session_logs(cfg, key)
    return {"archived": len(to_archive), "remaining": len(post), "cutoff_seq": cutoff,
            "pruned_logs": pruned_logs}


def archive_done(cfg: Config) -> list[str]:
    """Move DONE tickets out of the active scan into tickets/_archive/."""
    from .dispatcher import list_keys
    moved = []
    for key in list_keys(cfg.home):
        snap = snap_mod.load(cfg.home, key)
        if snap.phase == Phase.DONE.value:
            src = store.ticket_dir(cfg.home, key)
            dst = cfg.home / "tickets" / "_archive" / key
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                src.replace(dst)
            moved.append(key)
    return moved
