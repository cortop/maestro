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
import tempfile
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
    if key.endswith(".archive"):
        # events_path(home, "X.archive") == events_archive_path(home, "X") -- a
        # key ending in ".archive" silently aliases another key's compaction
        # archive file (RB-3). A pure suffix check, no filesystem access, so this
        # stays O(1) on the hot path (validate_key runs on every path construction).
        raise MaestroError(
            f"invalid ticket key: {key!r} (a trailing '.archive' would collide with "
            "another key's compaction-archive path)"
        )
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


def worktree_path(home: Path, key: str) -> Path:
    """Where *key*'s reconciler worktree lives. Flat under ``home/worktrees``
    (keys are home-unique) regardless of which repo the ticket is bound to --
    the git metadata inside the worktree already records its owning repo."""
    return home / "worktrees" / validate_key(key)


def scratch_path(home: Path, key: str) -> Path:
    """Per-key, non-git scratch cwd for a ``git``-mode reconciler before its
    worktree exists (QW-7): a plain directory, never the human's own shared
    checkout (``[repos.*].path``). Flat under ``home/scratch``, same layout
    rationale as ``worktree_path``."""
    return home / "scratch" / validate_key(key)


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


def atomic_write(target: Path, data: str, *, follow_symlinks: bool = False) -> None:
    """Write whole-file atomically: temp -> fsync -> rename -> fsync(dir).

    A reader (Obsidian, the dispatcher, another agent) always observes either the
    complete old file or the complete new one, never a torn write. See
    https://calvin.loncaric.us/articles/CreateFile.html

    ``follow_symlinks=True`` is opt-in, used only by ``config.write_scheduled``
    (GA-13 Part B). It resolves a symlinked ``target`` to its real file first, so
    the temp file lands beside -- and the rename replaces -- the symlink's TARGET,
    leaving the symlink itself intact. The default (False) is unchanged for every
    other caller: plain ``os.replace`` unlinks a destination symlink and replaces
    it with a real file in the symlink's own directory, which is what every other
    write site (derived/*, cursors, snapshots, claims, dashboards, the
    deadletter) wants -- resolving symlinks there would move the temp file into
    someone else's directory and turn a same-filesystem rename into a
    potentially cross-filesystem one. A failed replace (e.g. EXDEV from a
    cross-filesystem target, or ENOENT racing a concurrent unlink) is re-raised
    as a `MaestroError` with a clear message instead of a bare `OSError`, on
    every caller regardless of `follow_symlinks`, so it can't escape uncaught
    into a TUI callback (RB-5).

    The temp name comes from `tempfile.mkstemp`, not `f"...{os.getpid()}"` --
    the pid alone is unique across processes but not across threads of one
    process, and the TUI runs several `atomic_write` callers (`ops.compact`,
    the projection rebuild, fleet status) on Textual worker threads inside a
    single process. `mkstemp` guarantees each call gets its own name, so two
    threads racing on the same target can no longer compute the same temp
    path and step on each other's write (RB-5). It still lands in
    `target.parent` -- same-directory rename is what makes the atomicity
    argument hold, see the TRAPS note in RB-5's spec -- and its fd is closed
    immediately; the write below reopens that (already-unique, already ours)
    path so the rest of the write/fsync/replace sequence, and its
    failure-injection seam (`tests/fault_injection.py` patches `Path.open`),
    stays exactly as it was.
    """
    if follow_symlinks and target.is_symlink():
        target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.tmp.")
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    with tmp.open("w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, target)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise MaestroError(f"atomic_write: could not replace {target}: {e}") from e
    dfd = os.open(str(target.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def append_line(target: Path, line: str) -> None:
    """Durably append a single line (must hold ``file_lock`` for the stream).

    A previous writer may have died mid-line, leaving the file's last byte not a
    ``\\n``. Appending straight onto that (the old bug) concatenates our bytes onto
    the torn line instead of forming a new record -- silently swallowing this
    append and reusing its seq. So before writing, check (via a seek-to-end + a
    single 1-byte read -- O(1) in file size, never the whole file) whether the
    target is non-empty and its last byte isn't a newline; if so, prepend one. This
    is additive-only: the torn fragment is left in place for ``read_jsonl`` to skip
    as unparseable, never rewritten (rewriting would turn an append into a
    read-modify-write on the sole source of truth).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    needs_leading_newline = False
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        size = 0
    if size > 0:
        with target.open("rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_leading_newline = f.read(1) != b"\n"
    text = line if line.endswith("\n") else line + "\n"
    if needs_leading_newline:
        text = "\n" + text
    with target.open("a", encoding="utf-8") as f:
        f.write(text)
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
