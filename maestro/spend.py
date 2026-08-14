"""Per-day USD spend meter folded from session logs, and the ceiling gate it feeds.

The 2026-07-19 incident ($845 across 21,731 no-op spawns) was measured in dollars,
and nothing in the package was denominated in money -- ``daily_token_ceiling`` sat
parsed-and-unread (see GA-11). This module is the fix: it mirrors
:mod:`maestro.ratelimit`'s shape almost exactly (a byte-offset cursor over the same
``*.stream.jsonl`` session logs, keyed off the spawn ledger) but keeps its own
cursor/state files under different names, so the two gates stay independently
disable-able.

``probe`` runs on every real dispatcher sweep (via ``_run_hook``, never on a
cadence) and sums every un-drained stream-json session's terminal ``result``
record's ``total_cost_usd`` into a bucket keyed by the current UTC date
(``derived/.spend.json``) — not just each key's newest log, so an older log
left un-drained by a differently-formatted newer log still gets folded — reading
only bytes appended since the last sweep, per log (``derived/.spend_cursor.json``). A
session SIGTERM'd by ``run_watchdog`` before it can write a ``result`` record
would otherwise silently cost $0 in this meter -- once such a log has been
drained to its current end with no ``result`` ever seen AND its key is no
longer live (:func:`maestro.claims.active_keys`), it is counted explicitly as
``unattributed_sessions`` rather than folded into ``total_usd`` or dropped.

Sub-agent (``Agent``-tool) spend is already included: those run inside the
parent ``claude`` process, so the parent's ``result.total_cost_usd`` already
covers them -- this meter does not double-count GA-14's amplification.

``status`` is the read-only view ``maestro doctor``, the TUI, and the ceiling
gate in ``dispatch()`` all use -- unlike ``probe`` it never folds logs and never
raises: a lost or corrupt ``derived/.spend*.json`` must resume the fleet, not
wedge it, exactly like ``ratelimit.paused_until``. ``probe`` is the one call
allowed to raise on genuinely malformed state (routed through ``_run_hook`` so
the sweep survives it, per L-12) -- it deliberately does NOT guard every
``.get()`` the way ``status`` does.

A ``session_log_format = "text"`` home carries no parseable records (see
``ratelimit.py``'s own limitation note) -- its spend is reported as explicitly
``unavailable``, never a silent ``$0.00``, which would be a ceiling that can
never fire.

T-58 (AC8): a pi ``.pi.jsonl`` session log is folded here too -- it carries no
Claude-shaped ``result.total_cost_usd`` record, so its per-session total is
summed from every assistant ``message_end``'s ``message.usage.cost.total``
instead (one such record per turn; pi's own "terminal" marker for settling
purposes is ``agent_end``, not ``result`` -- see the per-candidate loop below).
``unavailable`` stays keyed on ``cfg.session_log_format`` ALONE -- a
Claude-log-capture knob, not a per-runner fact (this ticket's Notes flag this
explicitly): the default (``"stream-json"``) leaves pi folding fully working
alongside Claude's, but a board that deliberately sets ``session_log_format =
"text"`` reports the WHOLE meter unavailable even though pi's own capture is
unconditional (unlike Claude's) and could in principle still be summed. This is
a documented, conservative gap -- not a silent one -- left for a future ticket
that wants per-runner spend availability rather than one board-wide flag.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import claims, sessions as sessions_mod, steplog, store
from .config import Config


def _state_path(home: Path) -> Path:
    return home / "derived" / ".spend.json"


def _cursor_path(home: Path) -> Path:
    return home / "derived" / ".spend_cursor.json"


def _utc_date(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def probe(cfg: Config, now: float) -> dict:
    """One sweep's worth of spend folding. Reads only bytes appended since the
    last sweep from every spawn-ledger key's newest stream-json session log
    (mirrors ``ratelimit.probe``'s candidate selection), sums each session's
    terminal ``total_cost_usd`` into today's UTC-date bucket, and persists to
    ``derived/.spend.json``. Returns the resulting state dict.

    Callers MUST route this through ``dispatcher._run_hook`` (like
    ``ratelimit_probe``) -- unlike :func:`status`, this function does not guard
    against a state file that parses as valid JSON but the wrong shape (a bare
    string/list rather than an object); such garbage raises naturally on the
    first ``.get()``, by design, so a hook wrapper can record it on the report
    rather than this module silently swallowing corruption it can't make sense
    of.
    """
    home = cfg.home
    today = _utc_date(now)
    state_path = _state_path(home)

    if cfg.session_log_format != "stream-json":
        state = {"date": today, "total_usd": 0.0, "unattributed_sessions": 0,
                 "unavailable": True}
        store.write_json(state_path, state)
        return state

    raw = store.read_json(state_path, None)
    if raw is None:
        state = {"date": today, "total_usd": 0.0, "unattributed_sessions": 0,
                 "settled_logs": [], "unavailable": False}
    else:
        state = raw  # may raise below if this is garbage -- see docstring
        if state.get("date") != today:
            state = {"date": today, "total_usd": 0.0, "unattributed_sessions": 0,
                     "settled_logs": [], "unavailable": False}

    total = float(state.get("total_usd", 0.0) or 0.0)
    unattributed = int(state.get("unattributed_sessions", 0) or 0)
    settled = set(state.get("settled_logs", []) or [])

    # Lazy: dispatcher imports us at module load time, so importing dispatcher
    # back at our own module level would be a load-time cycle (mirrors
    # ratelimit.probe's identical constraint).
    from . import dispatcher as disp

    ledger = store.read_json(disp._spawn_ledger_path(home), {}) or {}
    cursor_path = _cursor_path(home)
    cursor = store.read_json(cursor_path, {}) or {}
    cursor_changed = False
    live_keys: set[str] | None = None  # resolved lazily, only if actually needed

    for key in ledger:
        candidates = sessions_mod.list_sessions(home, key)
        for candidate in candidates:
            fmt = candidate["format"]
            if fmt not in ("stream-json", "pi"):
                continue  # text-format (and opencode) logs carry no parseable cost records
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
                if fmt == "stream-json":
                    if record.get("type") == "result":
                        cost = record.get("total_cost_usd")
                        if isinstance(cost, (int, float)):
                            total += float(cost)
                        settled.add(log_id)
                else:  # T-58 (AC8): pi -- see module docstring for the shape
                    if record.get("type") == "agent_end":
                        settled.add(log_id)
                    elif record.get("type") == "message_end":
                        msg = record.get("message") or {}
                        if msg.get("role") == "assistant":
                            cost = ((msg.get("usage") or {}).get("cost") or {}).get("total")
                            if isinstance(cost, (int, float)):
                                total += float(cost)
            if pos != start:
                cursor[log_id] = pos
                cursor_changed = True
            # Trap: a session SIGTERM'd by run_watchdog mid-stream may never
            # write its terminal marker (`result` for stream-json, `agent_end`
            # for pi), so its cost would otherwise silently count as zero. Once
            # we've drained a log to its current end with no terminal marker
            # ever seen AND its key is no longer live, count it explicitly
            # instead of dropping it -- and mark it settled so we don't
            # recount the same dead log every subsequent sweep.
            if pos >= size and log_id not in settled:
                if live_keys is None:
                    live_keys = claims.active_keys(home)
                if key not in live_keys:
                    unattributed += 1
                    settled.add(log_id)

    if cursor_changed:
        store.write_json(cursor_path, cursor)

    state = {
        "date": today,
        "total_usd": round(total, 6),
        "unattributed_sessions": unattributed,
        "settled_logs": sorted(settled),
        "unavailable": False,
    }
    store.write_json(state_path, state)
    return state


def status(cfg: Config, now: float) -> dict:
    """Read-only view for ``maestro doctor`` / the TUI / the ``dispatch()``
    ceiling gate. Never folds logs (that's ``probe``'s job) and never raises --
    a lost or corrupt ``derived/.spend.json`` degrades to "unknown", not a
    crash, exactly like ``ratelimit.paused_until``.

    ``unavailable`` is derived from ``cfg.session_log_format`` directly, not
    the persisted state, so a ``text``-format home reads unavailable even
    before a sweep has ever probed.
    """
    today = _utc_date(now)
    result = {
        "date": today,
        "today_usd": None,
        "ceiling_usd": cfg.daily_spend_ceiling_usd,
        "unavailable": cfg.session_log_format != "stream-json",
        "unattributed_sessions": 0,
    }
    if result["unavailable"]:
        return result
    state = store.read_json(_state_path(cfg.home), None)
    if isinstance(state, dict) and state.get("date") == today and not state.get("unavailable"):
        total = state.get("total_usd", 0.0)
        result["today_usd"] = round(float(total), 2) if isinstance(total, (int, float)) else 0.0
        unattributed = state.get("unattributed_sessions", 0)
        result["unattributed_sessions"] = unattributed if isinstance(unattributed, int) else 0
    else:
        result["today_usd"] = 0.0
    return result


def over_ceiling(cfg: Config, now: float) -> str | None:
    """Reason string if today's spend is at or above ``daily_spend_ceiling_usd``,
    else ``None``. ``None`` ceiling (unset, the default) or an unavailable meter
    (text-format logs) both fail OPEN -- the gate never blocks on a number it
    cannot trust."""
    ceiling = cfg.daily_spend_ceiling_usd
    if ceiling is None:
        return None
    st = status(cfg, now)
    if st["unavailable"] or st["today_usd"] is None:
        return None
    if round(st["today_usd"], 2) >= round(float(ceiling), 2):
        return (f"daily spend ceiling reached: ${st['today_usd']:.2f} of "
                f"${float(ceiling):.2f}")
    return None
