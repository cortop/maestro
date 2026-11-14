"""Cross-ticket activity feed view-model -- a bounded recent window of every event log; stdlib only."""
from __future__ import annotations

import json as _json
from pathlib import Path

from .. import store
from .events import CATEGORY_NAMES, category_of, event_summary

TAIL_BYTES = 64 * 1024
PER_KEY = 40
LIMIT = 200


def _tail_events(path: Path, per_key: int) -> list[dict]:
    """The last *per_key* events of one log, read from its final ``TAIL_BYTES`` only."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, 2)
            start = max(0, size - TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if start:
        lines = lines[1:]  # the first line of a mid-file read is a fragment
    out: list[dict] = []
    for raw in lines[:-1] if not data.endswith(b"\n") else lines:  # a torn last line is skipped
        if not raw.strip():
            continue
        try:
            obj = _json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("seq"), int):
            out.append(obj)
    return out[-per_key:]


def recent(home: Path, *, cats: set[int] | None = None, text: str = "",
           limit: int = LIMIT, per_key: int = PER_KEY) -> list[dict]:
    """Newest-first rows ``{key, seq, ts, type, actor, summary, cat}`` over the recent window of
    every ticket. *cats* keeps only those category numbers (``None`` keeps all); *text* keeps rows
    whose key, type or summary contains it, case-insensitively."""
    d = Path(home) / "events"
    try:
        names = sorted(f.name for f in d.iterdir()
                       if f.name.endswith(".jsonl") and not f.name.endswith(".archive.jsonl")
                       and not f.name.startswith("."))
    except OSError:
        return []
    needle = text.strip().lower()
    rows: list[dict] = []
    for name in names:
        key = name[: -len(".jsonl")]
        for e in _tail_events(d / name, per_key):
            cat = category_of(str(e.get("type", "")))
            if cats is not None and cat not in cats:
                continue
            row = {"key": key, "seq": e["seq"], "ts": e.get("ts") or "", "type": str(e.get("type", "")),
                   "actor": str(e.get("actor", "")), "summary": event_summary(e), "cat": cat}
            if needle and needle not in f"{key} {row['type']} {row['summary']}".lower():
                continue
            rows.append(row)
    rows.sort(key=lambda r: (store.iso_to_epoch(r["ts"]) or 0, r["key"], r["seq"]), reverse=True)
    return rows[:limit]


__all__ = ["CATEGORY_NAMES", "recent"]
