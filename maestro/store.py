"""Filesystem primitives: home resolution, atomic writes, advisory locks.

Everything that must be *correct by construction* lives here. Agents never write
files directly — they go through the CLI, which goes through these helpers. That
is what makes "edit markdown while the orchestrator works" safe: humans append to
their own files; machines atomically replace their own files; the two never share
a read-modify-write window.
"""
from __future__ import annotations

import json
import os
import re
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# A ticket key is used in file paths, so it must be path-safe.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class MaestroError(Exception):
    """Base class for all maestro errors."""


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.match(key) or key in {".", ".."}:
        raise MaestroError(f"invalid ticket key: {key!r}")
    return key


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def resolve_home(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve MAESTRO_HOME: explicit arg > env > ~/.maestro."""
    raw = explicit or os.environ.get("MAESTRO_HOME") or "~/.maestro"
    return Path(raw).expanduser().resolve()


# ---------------------------------------------------------------------------
# Path layout (the whole state model lives under home)
# ---------------------------------------------------------------------------
def events_path(home: Path, key: str) -> Path:
    return home / "events" / f"{validate_key(key)}.jsonl"


def events_archive_path(home: Path, key: str) -> Path:
    return home / "events" / f"{validate_key(key)}.archive.jsonl"


def snapshot_path(home: Path, key: str) -> Path:
    return home / "derived" / "snapshots" / f"{validate_key(key)}.json"


def archived_snapshot_path(home: Path, key: str) -> Path:
    return home / "derived" / "snapshots" / "_archive" / f"{validate_key(key)}.json"


def archived_events_path(home: Path, key: str) -> Path:
    return home / "events" / "_archive" / f"{validate_key(key)}.jsonl"


def archived_events_archive_path(home: Path, key: str) -> Path:
    return home / "events" / "_archive" / f"{validate_key(key)}.archive.jsonl"


def cursor_path(home: Path, key: str) -> Path:
    return home / "derived" / "cursors" / f"{validate_key(key)}.json"


def inbox_path(home: Path, key: str) -> Path:
    return home / "inbox" / f"{validate_key(key)}.jsonl"


def new_inbox_path(home: Path) -> Path:
    return home / "inbox" / "_new.jsonl"


def new_cursor_path(home: Path) -> Path:
    return home / "derived" / "cursors" / "_new.json"


def spec_path(home: Path, key: str) -> Path:
    return home / "tickets" / validate_key(key) / "spec.md"


def ticket_dir(home: Path, key: str) -> Path:
    return home / "tickets" / validate_key(key)


def session_log_path(home: Path, key: str, session_id: str) -> Path:
    return home / "agent-logs" / validate_key(key) / f"{session_id}.log"


def session_stream_path(home: Path, key: str, session_id: str) -> Path:
    return home / "agent-logs" / validate_key(key) / f"{session_id}.stream.jsonl"


def deadletter_path(home: Path, key: str) -> Path:
    return home / "tickets" / "_deadletter" / f"{validate_key(key)}.md"


def _lock_file(target: Path) -> Path:
    return target.parent / f".{target.name}.lock"


# ---------------------------------------------------------------------------
# Locking + atomic writes
# ---------------------------------------------------------------------------
@contextmanager
def file_lock(target: Path) -> Iterator[None]:
    """Exclusive advisory lock keyed to ``target`` (held on a sidecar .lock file).

    Single-writer-per-stream: only one process appends to a given event log at a
    time. Different keys use different lock files, so distinct tickets never block
    each other.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_file(target)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write(target: Path, data: str) -> None:
    """Write whole-file atomically: temp -> fsync -> rename -> fsync(dir).

    A reader (Obsidian, the dispatcher, another agent) always observes either the
    complete old file or the complete new one, never a torn write. See
    https://calvin.loncaric.us/articles/CreateFile.html
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.tmp.{os.getpid()}"
    with tmp.open("w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    dfd = os.open(str(target.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def append_line(target: Path, line: str) -> None:
    """Durably append a single line (must hold ``file_lock`` for the stream)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(line if line.endswith("\n") else line + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(target: Path) -> list[dict]:
    out: list[dict] = []
    if not target.exists():
        return out
    with target.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                # Tolerate a half-written final line (a crashed writer). The next
                # fold simply ignores it; the durable append above makes this rare.
                continue
    return out


def write_json(target: Path, obj) -> None:
    atomic_write(target, json.dumps(obj, indent=2, sort_keys=True))


def read_json(target: Path, default=None):
    if not target.exists():
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
