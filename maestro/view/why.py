"""Why-screen data (T-165) -- read-only answers to "why isn't this ticket dispatched" -- no textual dependency."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .. import claims, depgraph, fleet as fleet_mod, ratelimit, snapshot as snap_mod, spend, store
from .. import dispatcher as disp
from ..config import Config
from ..statemachine import TERMINAL_PHASES, Phase


def fmt_wait(seconds: float) -> str:
    """Compact countdown: ``45s`` / ``4m00s`` / ``2h05m``."""
    s = max(0, int(seconds + 0.999))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def open_deps(home: Path, key: str) -> list[tuple[str, str]]:
    """``(dep, phase)`` for each dependsOn entry that is not yet terminal."""
    return [(dep, phase) for dep, _emoji, phase in depgraph.dep_status(home, key)
            if phase == "missing" or Phase(phase) not in TERMINAL_PHASES]


def now_verdict(home: Path, key: str, snap: snap_mod.Snapshot, now: float) -> tuple[disp.DueResult, str]:
    """The sweep's own due verdict (``dispatcher.due_check``) plus its one-line rendering."""
    res = disp.due_check(home, key, snap, now)
    if res.due:
        return res, f"DUE: {res.reason}"
    if res.reason == "backoff":
        wake = max(0.0, (snap.next_requeue_at or now) - now)
        return res, f"NOT DUE: backoff, wakes in {fmt_wait(wake)}"
    if res.reason == "blocked-dep":
        deps = ", ".join(f"{d} ({p})" for d, p in open_deps(home, key)) or "?"
        return res, f"NOT DUE: blocked-dep: {deps}"
    if res.reason == "terminal":
        return res, f"NOT DUE: terminal ({snap.phase})"
    return res, f"NOT DUE: {res.reason}"


def load_cfg(home: Path) -> Config:
    from .. import config as config_mod
    try:
        return config_mod.load(str(home))
    except store.MaestroError:
        return Config(home=home)


def gather_context(home: Path, key: str, cfg: Config, now: float) -> dict:
    """Everything that is blocking to read (ps probe, spend logs, ledger files):
    worker thread only. Never releases a claim -- ``describe_claims`` is read-only."""
    ledger = store.read_json(disp._spawn_ledger_path(home), {}) or {}
    entry = ledger.get(key) if isinstance(ledger, dict) else None
    attempts = store.read_json(disp._spawn_attempts_path(home), {}) or {}
    claim_row = next((r for r in claims.describe_claims(
        home, max_age=cfg.unverified_claim_max_age) if r["key"] == key), None)
    try:
        spend_reason = spend.over_ceiling(cfg, now)
    except Exception as exc:  # noqa: BLE001 -- an unreadable meter must not blank the screen
        spend_reason = f"spend meter unreadable: {exc}"
    return {
        "ledger": entry if isinstance(entry, dict) else (
            {"last": entry} if isinstance(entry, (int, float)) else None),
        "attempts": attempts.get(key) if isinstance(attempts, dict) else None,
        "claim": claim_row,
        "spend": spend_reason,
        "decisions": load_decisions(home, key),
    }


_decision_cache: dict[tuple[str, str], tuple[tuple[int, int] | None, list[dict]]] = {}


def load_decisions(home: Path, key: str, tail: int = 20) -> list[dict]:
    """``dispatcher.key_decisions`` cached by the ledger's (mtime, size): every
    ledger line carries every key's decision, so a re-parse is not free."""
    path = disp.dispatch_ledger_path(home)
    try:
        st = path.stat()
        sig: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    hit = _decision_cache.get((str(home), key))
    if hit is not None and hit[0] == sig:
        return hit[1]
    rows = disp.key_decisions(home, key, tail=tail) if sig is not None else []
    _decision_cache[(str(home), key)] = (sig, rows)
    return rows


def throttle_text(home: Path, key: str, snap: snap_mod.Snapshot, res: disp.DueResult,
                  cfg: Config, ctx: dict, now: float) -> str:
    """The Throttle line; mirrors ``_gate_due``'s floor exemptions."""
    parts: list[str] = []
    floor = disp.spawn_floor(cfg)
    entry = ctx.get("ledger")
    last = entry.get("last") if entry else None
    if not floor:
        parts.append("spawn floor off")
    elif res.reason in disp._UNTHROTTLED_REASONS:
        parts.append(f"floor bypassed ({res.reason})")
    elif entry and entry.get("phase") is not None and entry.get("phase") != snap.phase:
        parts.append(f"floor bypassed (phase hand-off {entry.get('phase')} -> {snap.phase})")
    elif isinstance(last, (int, float)) and now - last < floor:
        parts.append(f"next spawn allowed in {fmt_wait(last + floor - now)}")
    else:
        parts.append(f"spawn floor clear ({floor}s)")
    pause = fleet_mod.pause_state(home, now)
    if pause is not None:
        until = pause.get("until")
        parts.append("fleet PAUSED" + (f" ({pause['reason']})" if pause.get("reason") else "")
                     + (f" until {until}" if until else ""))
    rl = ratelimit.status(home, now)
    if rl.get("paused"):
        parts.append("rate-limited until " + datetime.fromtimestamp(rl["paused_until"]).strftime("%H:%M"))
    if ctx.get("spend"):
        parts.append(str(ctx["spend"]))
    return " · ".join(parts)


def claim_text(ctx: dict) -> str:
    row = ctx.get("claim")
    if row is None:
        return "no claim"
    age = f", age {fmt_wait(row['age_s'])}" if row.get("age_s") is not None else ""
    state = "live" if row["claimed"] else "STALE (a sweep would reap it)"
    return f"{state} claim, pid {row['pid']}{age}, {row['verdict']}"


def attempts_text(snap: snap_mod.Snapshot, cfg: Config, ctx: dict) -> str:
    entry = ctx.get("attempts")
    count = entry["count"] if entry and entry.get("seq") == snap.observed_seq else 0
    return f"spawn attempts at seq {snap.observed_seq}: {count}/{cfg.max_spawn_attempts}"


def outcome_style(outcome: str | None) -> str:
    o = outcome or ""
    if o == "spawned":
        return "green"
    if o == "not_due":
        return "dim"
    if o.startswith("would_"):
        return "cyan"
    if any(w in o for w in ("error", "fail", "park", "dead")):
        return "red"
    return "yellow"
