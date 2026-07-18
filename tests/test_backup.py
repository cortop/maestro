"""Backups guard the sole-source-of-truth event logs against an external wipe.

Every test drives the REAL surface: the ``maestro`` CLI over a temp home, or a
real dispatcher sweep — never a mocked backup module.
"""
import json
import shutil

import pytest

from maestro import backup, dispatcher as disp, store
from maestro.cli import main
from maestro.config import load
from maestro.sessions import DryRunSessions


def _run(home, *argv, capsys=None):
    """Invoke the real CLI entrypoint; return (exit_code, parsed_out, stderr)."""
    rc = main(["--home", str(home), *argv])
    cap = capsys.readouterr() if capsys else None
    out, err = (cap.out, cap.err) if cap else ("", "")
    try:
        return rc, json.loads(out), err
    except (json.JSONDecodeError, ValueError):
        return rc, out, err


def _reset_backups(cfg):
    """Clear snapshot dir + cursor so a test can drive the clock deterministically."""
    shutil.rmtree(backup.resolve_backup_dir(cfg), ignore_errors=True)
    (cfg.home / "derived" / ".backup_cursor.json").unlink(missing_ok=True)


def _populate(home, capsys):
    """A home with one real, minted ticket (T-1) driven through the CLI."""
    _run(home, "init")
    _run(home, "create", "First ticket", "--tier", "1", "--no-nudge", capsys=capsys)
    _run(home, "dispatch", "--dry-run", capsys=capsys)  # mints T-1 (DryRun: no spawn)
    assert (home / "events" / "T-1.jsonl").exists()
    assert (home / "tickets" / "T-1" / "spec.md").exists()


def test_backup_restore_roundtrip_after_wipe(tmp_path, capsys):
    home = tmp_path / "myhome"
    _populate(home, capsys)

    rc, created, _ = _run(home, "backup", capsys=capsys)
    assert rc == 0
    archive = created["created"]
    assert (home / "events" / "T-1.jsonl").read_text()  # unchanged by backup

    # Simulate the external `rm -rf` that lost the real board.
    for d in ("events", "tickets", "inbox", "derived"):
        shutil.rmtree(home / d, ignore_errors=True)
    (home / "config.toml").unlink()
    assert not (home / "events" / "T-1.jsonl").exists()

    rc, restored, _ = _run(home, "restore", capsys=capsys)
    assert rc == 0
    assert restored["restored_from"] == archive
    assert restored["keys"] == ["T-1"]

    # Source-of-truth + human-owned + config all back...
    assert (home / "events" / "T-1.jsonl").exists()
    assert (home / "tickets" / "T-1" / "spec.md").exists()
    assert (home / "config.toml").exists()
    # ...and disposable derived state was refolded from the restored log.
    assert (home / "derived" / "snapshots" / "T-1.json").exists()
    assert "T-1" in (home / "derived" / "WORKSTATE.md").read_text()


def test_backups_live_outside_the_home(tmp_path, capsys):
    """A `rm -rf <home>` must not take the backups with it."""
    home = tmp_path / "myhome"
    _populate(home, capsys)
    _run(home, "backup", capsys=capsys)

    cfg = load(str(home))
    backup_dir = backup.resolve_backup_dir(cfg)
    assert home not in backup_dir.parents and backup_dir != home
    assert backup.list_backups(cfg)  # a snapshot exists there

    shutil.rmtree(home)
    assert backup.list_backups(cfg)  # survived the wipe


def test_restore_refuses_to_clobber_without_force(tmp_path, capsys):
    home = tmp_path / "myhome"
    _populate(home, capsys)
    _run(home, "backup", capsys=capsys)

    # Board is live (non-empty events/tickets) — restore must refuse.
    rc, _, err = _run(home, "restore", capsys=capsys)
    assert rc == 2  # MaestroError -> exit 2
    assert "refusing to overwrite" in err

    rc, restored, _ = _run(home, "restore", "--force", capsys=capsys)
    assert rc == 0
    assert restored["keys"] == ["T-1"]


def test_dispatcher_auto_backup_is_interval_gated(tmp_path, capsys):
    home = tmp_path / "myhome"
    _populate(home, capsys)

    cfg = load(str(home))
    cfg.backup_interval = 3600
    _reset_backups(cfg)  # start from a clean, clock-controlled state
    t0 = 2_000_000_000.0

    # First sweep → one snapshot (fresh cursor).
    disp.dispatch(cfg, DryRunSessions(), now=t0)
    after_first = backup.list_backups(cfg)
    assert len(after_first) == 1

    # Second sweep within the interval → gated, no new snapshot.
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 100)
    assert backup.list_backups(cfg) == after_first

    # A sweep past the interval → a fresh snapshot.
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 4000)
    assert len(backup.list_backups(cfg)) == 2

    # backup_interval = 0 disables the hook entirely.
    cfg.backup_interval = 0
    disp.dispatch(cfg, DryRunSessions(), now=t0 + 999_999)
    assert len(backup.list_backups(cfg)) == 2


def test_retention_prunes_oldest(tmp_path, capsys):
    home = tmp_path / "myhome"
    _populate(home, capsys)
    cfg = load(str(home))
    cfg.backup_retention = 2
    _reset_backups(cfg)

    made = [backup.create_backup(cfg, now=2_000_000_000.0 + i * 10) for i in range(5)]
    kept = backup.list_backups(cfg)
    assert len(kept) == 2
    assert kept == sorted(made)[-2:]  # the two most recent survive
