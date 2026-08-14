"""Fleet-wide, account-wide spawn gate driven by the API's own ``rate_limit_event``
records — the single most expensive gap in the 2026-07-19 runaway ($658 of $845
burned spawning into a wall the fleet had already hit).

``probe`` runs on EVERY dispatcher sweep (never on a cadence, so a rejection is
noticed on the very next sweep, not after a whole idle interval). It takes its
candidate keys from the spawn ledger (``derived/.spawn_ledger.json`` — a rejected
session dies in under a second, so ``claims`` has already forgotten it by the next
sweep) and, for each key, drains every stream-json session log via
``sessions.list_sessions`` — not just the newest — so an older, still-un-drained
log is never skipped just because a differently-formatted log became newest in
the meantime. Only bytes appended since the last probe are read per log, via a
byte-offset cursor in ``derived/.ratelimit_cursor.json`` (persisted through
:func:`maestro.steplog.iter_records`, the repo's one stream-JSONL parser). On any
record with ``type == "rate_limit_event"`` and ``rate_limit_info.status ==
"rejected"`` it writes ``derived/.ratelimit.json``, keeping the max of any pause
already stored so a later, shorter-lived rejection can't shrink an active pause.

Fails OPEN by design: ``derived/`` is disposable, so a lost ``.ratelimit.json``
resumes the fleet rather than wedging it — the spawn-rate floor (dispatcher.py)
still bounds the rate on its own.

Limitation: only ``stream-json`` session logs carry parseable records, so a home
configured with ``session_log_format = "text"`` gets no gate — such keys are
skipped silently rather than erroring.

T-58 (AC9): a pi ``.pi.jsonl`` session log is deliberately left ungated here
too, on the SAME per-candidate ``format != "stream-json"`` check below — a
real captured pi stream (``tests/fixtures/*.pi.jsonl``) carries no analog of
Claude's ``rate_limit_event``/``rate_limit_info`` record (an auth failure
surfaces as an assistant message's ``stopReason: "error"`` /``errorMessage``
instead, which ``steplog.session_outcome`` already classifies for MTO-8's
purposes — see ``health.py``). This is an explicit, documented decision, not
an accident of the format string: the gate never crashes on nor
mis-parses a pi log (it is skipped exactly like a ``.opencode.jsonl`` or
``text`` one), and a future ticket can add real pi rate-limit extraction if
pi ever grows its own ``rate_limit_event``-shaped signal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import sessions as sessions_mod
from . import steplog, store
from .config import Config

_MS_THRESHOLD = 1e12  # resetsAt values beyond this are epoch milliseconds, not seconds


def _state_path(home: Path) -> Path:
    return home / "derived" / ".ratelimit.json"


def _cursor_path(home: Path) -> Path:
    return home / "derived" / ".ratelimit_cursor.json"


def _normalize_resets_at(value) -> float | None:
    """Coerce a raw ``resetsAt`` into epoch seconds, or ``None`` if unusable."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    v = float(value)
    if v > _MS_THRESHOLD:
        v /= 1000.0
    return v


def _compute_paused_until(resets_at_raw, now: float, cfg: Config) -> tuple[float, float | None]:
    """Return ``(paused_until, normalized_resets_at)``.

    Missing/non-numeric/already-past resetsAt falls back to
    ``ratelimit_fallback_pause`` seconds from now. Either path is then clamped to
    ``now + ratelimit_max_pause`` — a ``ratelimit_max_pause`` of 0 therefore
    clamps every pause to ``now``, disabling the gate entirely.
    """
    resets_at = _normalize_resets_at(resets_at_raw)
    if resets_at is None or resets_at <= now:
        pause_target = now + cfg.ratelimit_fallback_pause
    else:
        pause_target = resets_at + cfg.ratelimit_grace
    cap = now + cfg.ratelimit_max_pause
    if pause_target > cap:
        pause_target = cap
    return pause_target, resets_at


def probe(cfg: Config, now: float) -> dict | None:
    """One sweep's worth of rate-limit detection. Returns the active pause state
    (or ``None``), and persists it to ``derived/.ratelimit.json``."""
    home = cfg.home
    state_path = _state_path(home)
    state = store.read_json(state_path, None)
    if state and now >= state.get("paused_until", 0):
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass
        state = None

    # Lazy: dispatcher imports ratelimit at module load, so importing dispatcher
    # back at our own module level would be a load-time cycle.
    from . import dispatcher as disp

    ledger = store.read_json(disp._spawn_ledger_path(home), {}) or {}
    cursor_path = _cursor_path(home)
    cursor = store.read_json(cursor_path, {}) or {}
    cursor_changed = False
    best = state

    for key in ledger:
        candidates = sessions_mod.list_sessions(home, key)
        for candidate in candidates:
            if candidate["format"] != "stream-json":
                continue  # text/opencode/pi logs carry no parseable rate_limit_event (T-58: pi's own signal, if any, is unverified -- see module docstring)
            path = Path(candidate["path"])
            if not path.exists():
                continue
            log_id = str(path)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            start = cursor.get(log_id, 0)
            if not isinstance(start, (int, float)) or start > size:
                start = 0
            pos = start
            for offset, record in steplog.iter_records(path, start=start):
                pos = offset
                if record.get("type") != "rate_limit_event":
                    continue
                info = record.get("rate_limit_info") or {}
                if info.get("status") != "rejected":
                    continue
                pause_target, resets_at = _compute_paused_until(info.get("resetsAt"), now, cfg)
                if pause_target <= now:
                    continue  # ratelimit_max_pause == 0: gate disabled
                if best is None or pause_target > best.get("paused_until", 0):
                    best = {
                        "paused_until": pause_target,
                        "resets_at": resets_at,
                        "rate_limit_type": info.get("rateLimitType"),
                        "source_key": key,
                        "source_log": log_id,
                        "ts": store.iso_now(),
                    }
            if pos != start:
                cursor[log_id] = pos
                cursor_changed = True

    if cursor_changed:
        store.write_json(cursor_path, cursor)
    if best is not None and best is not state:
        store.write_json(state_path, best)
    return best


def paused_until(home: Path, now: float) -> float | None:
    """The fleet-wide pause deadline, or ``None`` if not currently paused."""
    state = store.read_json(_state_path(home), None)
    if not state:
        return None
    pu = state.get("paused_until")
    if pu is None or now >= pu:
        return None
    return pu


def _fmt_human(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def status(home: Path, now: float) -> dict:
    """Read-only view for ``maestro doctor`` / ``maestro ratelimit`` / the TUI."""
    state = store.read_json(_state_path(home), None)
    if not state or now >= state.get("paused_until", 0):
        return {"paused": False}
    pu = state["paused_until"]
    return {
        "paused": True,
        "paused_until": pu,
        "resets_at": state.get("resets_at"),
        "rate_limit_type": state.get("rate_limit_type"),
        "until": _fmt_human(pu),
    }


def clear(home: Path) -> bool:
    """Remove any active pause. Returns True if one was actually cleared."""
    path = _state_path(home)
    existed = path.exists()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return existed
