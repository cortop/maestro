"""Per-key burn detection (RB-11): the guard the 2026-08-14 T-55/T-56 incident
(116 sessions, $8.63, zero progress across 6.5 hours, invisible outside raw
``agent-logs/*.stream.jsonl``) needed and didn't have. Surfaces per-key spend
over a trailing window -- alongside ``health.spawn_rate``'s existing per-key
``by_key`` breakdown, which already covers the spawn side -- and flags a key
that is burning by either of two independent, cost-data-free signals cheap
enough to fire within the first hour (spec Notes, cheapest first):

1. ``repeated_failure`` -- a key's last ``burn_repeat_threshold`` ``Failed``
   events all carry byte-identical text. This is the exact measured T-55/T-56
   shape (fifteen identical ``watchdog: 5 spawns with no progress ...``
   errors) and needs no cost data at all.
2. ``_seq_stalled_by_key`` -- a key has been respawned ``burn_repeat_threshold``
   times at the SAME ``observed_seq`` (fed by the dispatcher's own no-progress
   watchdog ledger, ``.spawn_attempts.json``, GA-8). Deliberately does NOT
   overlap with signal 1: a session that appends a ``Failed`` event every
   attempt (T-55/T-56's own shape) advances ``observed_seq`` each time and so
   never accumulates here -- this catches the OTHER failure mode instead, a
   spawn that produces no event at all (a crash loop, or a free runner that
   never calls a ``maestro`` verb -- spec AC3's "cost: 0" case).

Neither signal is spend-based, by design (spec Notes: "a free runner can loop
just as hard and waste wall-clock and machine ... spend alone is not a
sufficient signal") -- ``per_key_spend`` below is pure visibility, folded into
``maestro doctor --json`` beside the existing ``spawns_last_hour.by_key`` so a
human never again has to hand-tally ``total_cost_usd`` out of raw session
logs to see where the money went.

``should_park`` is the bounding half (spec Notes: "a per-key rate cap that
parks a key rather than failing the board") -- config-gated by
``cfg.burn_repeat_threshold`` (0 disables it, independent of the WARN above),
it tells the dispatcher's spawn loop to dead-letter ONE burning key instead of
spawning it, without touching any other due key on the board. Only the
repeated-identical-failure signal parks today; the no-progress-spawn-count
signal is visibility only -- a single stuck attempt can legitimately be a
slow but healthy step (a long test suite, a big rebase), not a burn.

Neither signal can ever flag a key that is genuinely making progress: a
``Failed`` text that changes between attempts breaks signal 1's
byte-equality, and any event append at all -- Failed or otherwise -- advances
``observed_seq`` and resets signal 2's bucket (``dispatcher._allow_spawn``).
"""
from __future__ import annotations

from pathlib import Path

from . import dispatcher, event_log, events as E, sessions as sessions_mod, snapshot as snap_mod, steplog, store
from . import health as health_mod


def per_key_spend(home: Path, now: float, window: int = health_mod.WINDOW_SECONDS) -> dict[str, float]:
    """USD spent per key in the trailing *window* seconds, off each key's own
    stream-json session logs' terminal ``total_cost_usd`` -- a windowed
    sibling of ``spend.py``'s cumulative day-bucket meter, keyed per-ticket
    instead of board-wide. A pi/opencode session (no parseable Claude-shaped
    cost record, or a genuinely free ``cost: 0`` run) simply contributes
    nothing here -- never the sole burn signal, see the module docstring."""
    out: dict[str, float] = {}
    for key in dispatcher.list_keys(home):
        total = 0.0
        for entry in sessions_mod.list_sessions(home, key):
            if entry["format"] != "stream-json" or now - entry["epoch"] > window:
                continue
            result = steplog.session_outcome(Path(entry["path"])).get("result") or {}
            cost = result.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                total += float(cost)
        if total:
            out[key] = round(total, 6)
    return out


def _seq_stalled_by_key(home: Path, threshold: int) -> dict[str, int]:
    """Keys whose ``.spawn_attempts.json`` bucket (``dispatcher._allow_spawn``'s
    own {seq, count} ledger, reset the instant ``observed_seq`` changes) has
    reached *threshold* spawns at the same ``observed_seq`` -- see the module
    docstring's signal 2.

    ``_allow_spawn`` only resets a stale bucket lazily, on the KEY'S NEXT spawn
    attempt -- so a bucket recorded against an OLDER ``observed_seq`` than the
    key's current, live snapshot must not be trusted here: the key may have
    genuinely progressed since, and doctor reads this on demand, not only
    right before a spawn. Re-checked against the live snapshot on every call
    (spec AC4: "never flagged, however many spawns it consumes")."""
    if not threshold:
        return {}
    attempts = store.read_json(dispatcher._spawn_attempts_path(home), {}) or {}
    out: dict[str, int] = {}
    for key, entry in attempts.items():
        if not isinstance(entry, dict):
            continue
        count = entry.get("count")
        if not isinstance(count, int) or count < threshold:
            continue
        if entry.get("seq") == snap_mod.load(home, key).observed_seq:
            out[key] = count
    return out


def _recent_failure_texts(home: Path, key: str, n: int) -> list[str]:
    """The last *n* ``Failed`` error texts from *key*'s CURRENT phase visit
    only -- scoped to events after the most recent ``PhaseChanged``, mirroring
    ``Snapshot.failure_count``/``burning``'s own reset-on-phase-change
    semantics (``snapshot.py``). Without this scoping, a stale identical-
    failure streak from a phase the key has since LEFT (a human answering its
    question, a fresh fix round) could park a key that is no longer stuck."""
    evs = event_log.read(home, key)
    last_phase_change = max(
        (i for i, ev in enumerate(evs) if ev.get("type") == E.PHASE_CHANGED), default=-1)
    texts = [ev["payload"].get("error") for ev in evs[last_phase_change + 1:]
             if ev.get("type") == E.FAILED and isinstance(ev.get("payload"), dict)]
    return texts[-n:]


def repeated_failure(home: Path, key: str, threshold: int) -> str | None:
    """The repeated error text if *key*'s last *threshold* ``Failed`` events
    are all byte-identical (and non-empty), else ``None`` -- see the module
    docstring's signal 1. Fewer than *threshold* ``Failed`` events on record
    never trips this, however many spawns the key has otherwise consumed."""
    if not threshold:
        return None
    texts = _recent_failure_texts(home, key, threshold)
    if len(texts) < threshold:
        return None
    first = texts[0]
    return first if first and all(t == first for t in texts) else None


def report(cfg, now: float) -> dict:
    """The doctor-facing burn payload: per-key spend over the window plus the
    two independent flag maps -- a key in either is "burning". ``flagged`` is
    their sorted union, the list both ``health.check_burn`` and
    ``dispatcher``'s park gate key off of."""
    home = cfg.home
    threshold = cfg.burn_repeat_threshold
    repeated = {key: text for key in dispatcher.list_keys(home)
                if (text := repeated_failure(home, key, threshold))}
    no_progress = _seq_stalled_by_key(home, threshold)
    return {
        "spend_usd_by_key": per_key_spend(home, now),
        "repeated_failure_by_key": repeated,
        "no_progress_by_key": no_progress,
        "flagged": sorted(set(repeated) | set(no_progress)),
    }


def should_park(cfg, key: str) -> str | None:
    """Reason string if *key* should be dead-lettered instead of spawned THIS
    sweep -- the bounding half of RB-11. Config-gated by
    ``cfg.burn_repeat_threshold`` (0 disables the cap; doctor's WARN above
    stays independent of this gate). See the module docstring for why only
    the repeated-identical-failure signal parks."""
    threshold = cfg.burn_repeat_threshold
    if not threshold:
        return None
    text = repeated_failure(cfg.home, key, threshold)
    if text:
        return f"burn: {threshold} consecutive identical failures -- {text!r}"
    return None
