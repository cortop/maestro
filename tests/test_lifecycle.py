"""End-to-end-ish: a ticket walks the lifecycle, and crash-safety holds."""
from maestro import dispatcher as disp
from maestro import event_log, inbox, ops, projection, snapshot as snap_mod, store
from maestro.statemachine import Phase


def _create(cfg, key):
    store.atomic_write(store.spec_path(cfg.home, key), f"# {key}\napproval_tier: 1\n")
    event_log.append(cfg.home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(cfg.home, key)}, actor="d")
    snap_mod.rebuild(cfg.home, key)


def test_ask_then_answer_flow(cfg):
    home = cfg.home
    _create(cfg, "T-1")
    # reconcile #1: triage -> ask -> sleep
    ops.ask(cfg, "T-1", "Can I pick this up?", qid="q1")
    assert snap_mod.load(home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert not disp.is_due(home, "T-1", snap_mod.load(home, "T-1"), inbox_pending=False,
                           current_spec_hash=disp.spec_hash_on_disk(home, "T-1"), now=1).due

    # human answers
    inbox.append_command(home, "T-1", "ans", {"text": "yes", "qid": "q1"})
    assert inbox.has_pending(home, "T-1")

    # reconcile #2: fold inbox -> advance -> ack
    ops.fold_inbox(cfg, "T-1")
    assert snap_mod.load(home, "T-1").question_open is False
    ops.set_phase(cfg, "T-1", Phase.READY, reason="approved")
    inbox.ack(home, "T-1")
    assert snap_mod.load(home, "T-1").phase == Phase.READY.value
    assert not inbox.has_pending(home, "T-1")


def test_crash_before_ack_is_safe(cfg):
    """If a reconcile folds the inbox but dies before acking, re-running is a no-op
    (idempotent), not a double-application."""
    home = cfg.home
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "ok?", qid="q1")
    inbox.append_command(home, "T-1", "ans", {"text": "yes", "qid": "q1"})

    ops.fold_inbox(cfg, "T-1")        # crash happens right after this (no ack)
    seq_after_first = snap_mod.load(home, "T-1").observed_seq
    ops.fold_inbox(cfg, "T-1")        # re-spawn re-folds
    seq_after_second = snap_mod.load(home, "T-1").observed_seq
    assert seq_after_first == seq_after_second  # no duplicate events


def test_failure_backoff_then_deadletter(cfg):
    home = cfg.home
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING)
    assert ops.fail(cfg, "T-1", "boom").startswith("backoff")
    assert ops.fail(cfg, "T-1", "boom").startswith("backoff")
    result = ops.fail(cfg, "T-1", "boom")  # 3rd failure hits max_failures=3
    assert result == "dead-letter"
    assert snap_mod.load(home, "T-1").phase == Phase.DEGRADED.value
    assert store.deadletter_path(home, "T-1").exists()


def test_fail_dead_letter_skips_backoff_on_first_offense(cfg):
    """T-45: `dead_letter=True` dead-letters on THIS call, ignoring
    `max_failures` entirely -- for a structural failure a retry can't fix."""
    home = cfg.home
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING)
    result = ops.fail(cfg, "T-1", "boom", dead_letter=True)  # 1st-ever failure
    assert result == "dead-letter"
    assert snap_mod.load(home, "T-1").failure_count == 1
    assert snap_mod.load(home, "T-1").phase == Phase.DEGRADED.value
    assert store.deadletter_path(home, "T-1").exists()


def test_projection_never_reads_human_files(cfg):
    home = cfg.home
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "decide?", qid="q1")
    written = projection.write(home)
    needs = (home / "derived" / "NEEDS-YOU.md").read_text()
    assert "T-1" in needs and "decide?" in needs
    assert "NEEDS-YOU.md" in written and "DO NOT EDIT" in needs


def test_finalize_and_archive(cfg):
    home = cfg.home
    _create(cfg, "T-1")
    ops.finalize(cfg, "T-1")
    assert snap_mod.load(home, "T-1").phase == Phase.DONE.value
    moved = ops.archive_done(cfg)
    assert "T-1" in moved
    assert (home / "tickets" / "_archive" / "T-1").exists()


def test_archive_done_relocates_events_and_snapshot(cfg):
    """L-12 AC4: archive_done must relocate events/<KEY>.jsonl + the snapshot
    too, not just the ticket dir -- that's what makes `list_keys` stop
    sweeping an archived key."""
    home = cfg.home
    _create(cfg, "T-1")
    ops.finalize(cfg, "T-1")
    assert store.events_path(home, "T-1").exists()
    assert store.snapshot_path(home, "T-1").exists()

    moved = ops.archive_done(cfg)
    assert moved == ["T-1"]

    assert not store.events_path(home, "T-1").exists()
    assert not store.snapshot_path(home, "T-1").exists()
    assert store.archived_events_path(home, "T-1").exists()
    assert store.archived_snapshot_path(home, "T-1").exists()
    assert "T-1" not in disp.list_keys(home)


def test_archived_dependency_still_resolves_as_done(cfg):
    """A dependent must not block forever just because its dependency finished
    and got archived -- snapshot.load falls back to the archived location."""
    home = cfg.home
    _create(cfg, "T-dep")
    ops.finalize(cfg, "T-dep")
    ops.archive_done(cfg)
    assert "T-dep" not in disp.list_keys(home)

    _create(cfg, "T-1")
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 0\ndependsOn: [T-dep]\n")
    assert disp._has_unmet_deps(home, "T-1") is False
