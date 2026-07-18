"""Point-in-time backups of a home's irreplaceable state — the guard against an
external ``rm`` wiping the sole-source-of-truth event logs.

Only the non-derived, can-never-be-regenerated state is captured: the event logs
(``events/``), the human-owned specs (``tickets/``), the human inbox (``inbox/``)
and ``config.toml``. ``derived/``, ``agent-logs/`` and ``worktrees/`` are all
disposable or reconstructable, so they are deliberately excluded to keep snapshots
small. Backups default to a *sibling* of the home (never inside it), so a
``rm -rf <home>`` leaves them intact.
"""
from __future__ import annotations

import tarfile
from datetime import datetime, timezone
from pathlib import Path

from . import projection, snapshot as snap_mod, store
from .config import Config

# The irreplaceable, non-derived state a wipe can never bring back.
BACKUP_MEMBERS = ("events", "tickets", "inbox", "config.toml")
_PREFIX = "maestro-backup-"
_SUFFIX = ".tar.gz"


def resolve_backup_dir(cfg: Config) -> Path:
    """Where snapshots live. Default is a sibling of the home (NOT inside it), so
    ``rm -rf <home>`` leaves the backups untouched."""
    if cfg.backup_dir:
        return Path(cfg.backup_dir).expanduser()
    home = cfg.home
    return home.parent / f"{home.name}-backups"


def _stamp(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def list_backups(cfg: Config) -> list[Path]:
    """Existing snapshot tarballs, oldest first (name sorts chronologically)."""
    d = resolve_backup_dir(cfg)
    if not d.exists():
        return []
    return sorted(
        p for p in d.iterdir()
        if p.name.startswith(_PREFIX) and p.name.endswith(_SUFFIX) and p.is_file()
    )


def prune_backups(cfg: Config) -> list[Path]:
    """Keep only the most-recent ``backup_retention`` snapshots. 0/None = keep all."""
    keep = cfg.backup_retention
    if not keep or keep <= 0:
        return []
    backups = list_backups(cfg)
    removed: list[Path] = []
    for old in backups[:-keep] if keep < len(backups) else []:
        old.unlink(missing_ok=True)
        removed.append(old)
    return removed


def create_backup(cfg: Config, now: float) -> Path:
    """Write one tarball of the irreplaceable state, then prune to retention.

    Written to a temp name and renamed into place so a reader never sees a torn
    archive. Returns the archive path.
    """
    home = cfg.home
    dest_dir = resolve_backup_dir(cfg)
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / f"{_PREFIX}{_stamp(now)}{_SUFFIX}"
    tmp = dest_dir / f".{archive.name}.tmp"
    with tarfile.open(tmp, "w:gz") as tar:
        for member in BACKUP_MEMBERS:
            src = home / member
            if src.exists():
                tar.add(src, arcname=member)
    tmp.replace(archive)
    prune_backups(cfg)
    return archive


def maybe_backup(cfg: Config, now: float) -> Path | None:
    """Dispatcher hook: create a snapshot at most once per ``backup_interval``
    seconds, gated by a persisted cursor (level-triggered, idempotent — mirrors
    ``sync_external_sources``). No-op when disabled or when there is nothing yet
    worth protecting."""
    interval = cfg.backup_interval
    if not interval or interval <= 0:
        return None
    if not any((cfg.home / m).exists() for m in ("events", "tickets")):
        return None
    cursor_path = cfg.home / "derived" / ".backup_cursor.json"
    cursor = store.read_json(cursor_path, {}) or {}
    if now - cursor.get("epoch", 0) < interval:
        return None
    archive = create_backup(cfg, now)
    cursor["epoch"] = now
    cursor["archive"] = str(archive)
    store.write_json(cursor_path, cursor)
    return archive


def _safe_members(tar: tarfile.TarFile, base: Path) -> list[tarfile.TarInfo]:
    """Reject absolute paths / traversal — extraction must stay inside the home."""
    root = base.resolve()
    out: list[tarfile.TarInfo] = []
    for m in tar.getmembers():
        target = (root / m.name).resolve()
        if target != root and root not in target.parents:
            raise store.MaestroError(f"unsafe path in backup archive: {m.name!r}")
        out.append(m)
    return out


def _rebuild_all(home: Path) -> list[str]:
    """Refold every restored key's snapshot from its event log (derived state is
    disposable and not carried in the tarball)."""
    keys: set[str] = set()
    ev = home / "events"
    if ev.exists():
        for f in ev.iterdir():
            if (f.name.endswith(".jsonl") and not f.name.endswith(".archive.jsonl")
                    and not f.name.startswith(".")):
                keys.add(f.name[: -len(".jsonl")])
    tk = home / "tickets"
    if tk.exists():
        for d in tk.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                keys.add(d.name)
    rebuilt: list[str] = []
    for key in sorted(keys):
        try:
            snap_mod.rebuild(home, key)
            rebuilt.append(key)
        except store.MaestroError:
            continue
    return rebuilt


def restore_backup(cfg: Config, archive: Path | None = None, *, force: bool = False) -> dict:
    """Extract a backup into the home, then refold snapshots + dashboards.

    With no ``archive`` the latest snapshot is used. Refuses to overwrite a
    non-empty ``events/``/``tickets/`` unless ``force`` — so a mistaken restore
    can't clobber a live board.
    """
    home = cfg.home
    if archive is None:
        backups = list_backups(cfg)
        if not backups:
            raise store.MaestroError(f"no backups found in {resolve_backup_dir(cfg)}")
        archive = backups[-1]
    archive = Path(archive)
    if not archive.exists():
        raise store.MaestroError(f"backup not found: {archive}")

    if not force:
        for m in ("events", "tickets"):
            d = home / m
            if d.exists() and any(d.iterdir()):
                raise store.MaestroError(
                    f"{d} is non-empty; refusing to overwrite without --force")

    home.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(home, members=_safe_members(tar, home))

    rebuilt = _rebuild_all(home)
    projection.write(home)
    return {"restored_from": str(archive), "keys": rebuilt}
