"""Per-key human inbox — append-only, never clobbered.

The human's ONLY write channels are:
  * append a command to ``inbox/<KEY>.jsonl`` (via ``maestro ans`` / ``maestro cmd``)
  * append a create request to ``inbox/_new.jsonl`` (via ``maestro create``)
  * edit the human-owned ``tickets/<KEY>/spec.md``

Because these are append-only (or a file no agent rewrites), a human edit can never
land in the read-modify-write window of an agent. The reconciler *consumes* pending
commands by folding them into events and advancing a machine-owned cursor — it never
edits the inbox itself.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import store


def append_command(home: Path, key: str, command: str, args: dict | None = None) -> dict:
    path = store.inbox_path(home, key)
    entry = {"ts": store.iso_now(), "command": command, "args": args or {}}
    with store.file_lock(path):
        store.append_line(path, json.dumps(entry, separators=(",", ":")))
    return entry


def append_new(home: Path, title: str, key: str | None = None, args: dict | None = None,
               prefix: str | None = None) -> dict:
    """Queue a create-ticket request; the dispatcher mints the key from prefix or falls back to T-N."""
    path = store.new_inbox_path(home)
    entry = {"ts": store.iso_now(), "title": title, "key": key, "prefix": prefix, "args": args or {}}
    with store.file_lock(path):
        store.append_line(path, json.dumps(entry, separators=(",", ":")))
    return entry


def _cursor(path: Path) -> int:
    return int(store.read_json(path, {"consumed": 0}).get("consumed", 0))


def pending(home: Path, key: str) -> list[dict]:
    """Commands appended since the cursor (what this reconcile must fold)."""
    all_cmds = store.read_jsonl(store.inbox_path(home, key))
    return all_cmds[_cursor(store.cursor_path(home, key)):]


def has_pending(home: Path, key: str) -> bool:
    total = len(store.read_jsonl(store.inbox_path(home, key)))
    return total > _cursor(store.cursor_path(home, key))


def ack(home: Path, key: str) -> int:
    """Advance the cursor to the current inbox length. Returns new cursor."""
    total = len(store.read_jsonl(store.inbox_path(home, key)))
    store.write_json(store.cursor_path(home, key), {"consumed": total})
    return total


def pending_new(home: Path) -> list[tuple[int, dict]]:
    """(index, entry) for create-requests past the global _new cursor."""
    entries = store.read_jsonl(store.new_inbox_path(home))
    start = _cursor(store.new_cursor_path(home))
    return list(enumerate(entries))[start:]


def ack_new(home: Path) -> int:
    total = len(store.read_jsonl(store.new_inbox_path(home)))
    store.write_json(store.new_cursor_path(home), {"consumed": total})
    return total
