"""RB-3: two distinct ticket keys must not silently share one event log.

Two aliasing paths, both from the spec:

  (g) SUFFIX ALIASING -- a key ending in ".archive" collides with another
      key's compaction archive: ``events_path(h, "X.archive") ==
      events_archive_path(h, "X")``. ``store.validate_key`` now rejects this
      outright -- a pure suffix check, O(1), no filesystem access.

  (h) CASE ALIASING -- on a case-insensitive filesystem (APFS, the macOS
      default), "CASE-1" and "case-1" resolve to the same file. Detecting this
      needs a filesystem scan, which does NOT belong on validate_key's hot
      path -- it lives only in ``dispatcher.mint_new_tickets``, at
      ticket-creation time.

Either rejection is reported via the existing dead-letter surface
(``tickets/_deadletter/*.md``, already surfaced by ``maestro doctor``) instead
of the old ``except MaestroError: continue`` silent drop.
"""
import os

import pytest

from maestro import cli, dispatcher as disp, event_log, health, store
from maestro.sessions import DryRunSessions

# --- AC: `validate_key` rejects a `.archive`-suffixed key, and stays O(1) ---


def test_validate_key_rejects_archive_suffix():
    with pytest.raises(store.MaestroError, match=r"\.archive"):
        store.validate_key("X.archive")


def test_validate_key_does_not_touch_the_filesystem(monkeypatch):
    """The new suffix check is a pure string comparison -- keep validate_key
    off the filesystem, since it runs on every path construction (store.py's
    events_path/spec_path/etc, all hot paths)."""
    def _boom(*_a, **_k):
        raise AssertionError("validate_key must not touch the filesystem")

    monkeypatch.setattr(os, "stat", _boom)
    monkeypatch.setattr(os, "scandir", _boom)
    monkeypatch.setattr(os, "listdir", _boom)
    for key in ("T-1", "X.archive", "A" * 200, "..", "bad/slash", "CASE-1"):
        try:
            store.validate_key(key)
        except store.MaestroError:
            pass  # rejection is fine here -- only a filesystem touch is not


# --- AC: every existing key on the dogfood board still validates -----------

# The literal key set on the live self-dev board (`~/.maestro/maestro-dev/tickets`)
# as of 2026-08-09. Enumerated here rather than scanned live -- tests must
# never touch a real MAESTRO_HOME (CLAUDE.md) -- so this must not break any of
# them.
_DOGFOOD_BOARD_KEYS = [
    "AD-1", "AD-2", "AD-3", "AD-4", "AD-5", "AD-6",
    "GA-1", "GA-2", "GA-3", "GA-4", "GA-5", "GA-6", "GA-7", "GA-8", "GA-9",
    "GA-10", "GA-11", "GA-12", "GA-13", "GA-14", "GA-15", "GA-16", "GA-17",
    "GA-18", "GA-19", "GA-20", "GA-21",
    "L-12",
    "M-1", "M-12", "M-13",
    "MR-1", "MR-2", "MR-3", "MR-4", "MR-5", "MR-6",
    "RB-1", "RB-2", "RB-3", "RB-4", "RB-5", "RB-6", "RB-7", "RB-8", "RB-9",
    "RB-10", "RB-11", "RB-12",
    "T-1", "T-2", "T-3", "T-4", "T-10", "T-11", "T-12", "T-13", "T-14",
    "T-15", "T-16", "T-17", "T-18", "T-19", "T-20", "T-21", "T-22", "T-23",
    "T-24", "T-25",
]


@pytest.mark.parametrize("key", _DOGFOOD_BOARD_KEYS)
def test_validate_key_accepts_every_dogfood_board_key(key):
    assert store.validate_key(key) == key


# --- AC: a `.archive`-suffixed key is rejected through the real `create` path -


def test_archive_suffix_key_rejected_via_real_create_path(home, cfg):
    rc = cli.main(["--home", str(home), "create", "Colliding key",
                    "--key", "X.archive", "--no-nudge"])
    assert rc == 0  # queuing always succeeds -- the key is validated at mint time

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert "X.archive" not in report.minted
    # can't go through store.ticket_dir/events_path here -- they call
    # validate_key too, which now rejects this same key
    assert not (home / "tickets" / "X.archive").exists()
    assert not (home / "events" / "X.archive.jsonl").exists()

    dl = list((home / "tickets" / "_deadletter").glob("*.md"))
    assert dl, "a rejected create request must be reported, not silently dropped"
    body = dl[0].read_text()
    assert "X.archive" in body and "archive" in body.lower()

    # observable through a real CLI surface
    rpt = health.report(cfg, 1000)
    assert rpt["dead_letters"], "maestro doctor must surface the rejected create"


# --- AC: a key differing only by case from an existing key is rejected -----


def _filesystem_is_case_insensitive(tmp_path) -> bool:
    probe = tmp_path / "case-probe.txt"
    probe.write_text("x")
    return (tmp_path / "CASE-PROBE.txt").exists()


def test_case_colliding_key_rejected_on_case_insensitive_filesystem(home, cfg, tmp_path):
    if not _filesystem_is_case_insensitive(tmp_path):
        pytest.skip("filesystem is case-sensitive -- case aliasing cannot occur here")

    rc = cli.main(["--home", str(home), "create", "Original",
                    "--key", "case-1", "--no-nudge"])
    assert rc == 0
    first = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "case-1" in first.minted

    rc = cli.main(["--home", str(home), "create", "Colliding",
                    "--key", "CASE-1", "--no-nudge"])
    assert rc == 0
    second = disp.dispatch(cfg, DryRunSessions(), now=1001)

    assert "CASE-1" not in second.minted
    # the bug this ticket closes: both tickets' events landing in one stream
    assert len(event_log.read(home, "case-1")) == 1

    dl = list((home / "tickets" / "_deadletter").glob("*.md"))
    assert dl
    assert any("CASE-1" in p.read_text() for p in dl)


# --- AC: a rejected create request never lets an unsafe key touch the ------
# --- filesystem outside the dead-letter directory ---------------------------


def test_malformed_key_rejection_stays_inside_deadletter_dir(home, cfg):
    """A create-request's key is unvalidated user input at queue time (RB-3
    trap): reporting a rejection must not trust it as a raw path component,
    or the report itself reopens the traversal hole validate_key exists to
    close."""
    rc = cli.main(["--home", str(home), "create", "Bad key",
                    "--key", "../../etc/evil", "--no-nudge"])
    assert rc == 0

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert not report.minted
    assert not (home.parent / "etc").exists()

    dl_dir = home / "tickets" / "_deadletter"
    files = list(dl_dir.glob("*.md"))
    assert files
    for f in files:
        assert f.resolve().parent == dl_dir.resolve()
