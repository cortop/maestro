"""End-to-end-ish: a ticket walks the lifecycle, and crash-safety holds."""
import pytest

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


def test_ask_refuses_to_reuse_a_resolved_qid(cfg):
    """T-2, 2026-09-09: once a qid has been asked and answered, reusing it
    would silently no-op the QuestionAsked append (event_log's step_id dedup)
    yet still flip the ticket to awaiting-human with nothing newly open --
    exactly the shape that stranded a real ticket in a `triaging <->
    awaiting-human` loop forever. `ask` must raise instead."""
    home = cfg.home
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Can I pick this up?", qid="q1")
    inbox.append_command(home, "T-1", "ans", {"text": "yes", "qid": "q1"})
    ops.fold_inbox(cfg, "T-1")
    assert snap_mod.load(home, "T-1").question_open is False

    with pytest.raises(store.MaestroError, match="q1"):
        ops.ask(cfg, "T-1", "Can I pick this up again?", qid="q1")


def test_ask_round_refuses_to_reuse_a_resolved_qid(cfg):
    home = cfg.home
    _create(cfg, "T-1")
    ops.ask_round(cfg, "T-1", [("Q1?", None, "q1"), ("Q2?", None, "q2")])
    inbox.append_command(home, "T-1", "ans", {"text": "a1", "qid": "q1"})
    inbox.append_command(home, "T-1", "ans", {"text": "a2", "qid": "q2"})
    ops.fold_inbox(cfg, "T-1")
    assert snap_mod.load(home, "T-1").question_open is False

    with pytest.raises(store.MaestroError, match="q1"):
        ops.ask_round(cfg, "T-1", [("Q1 again?", None, "q1"), ("Q3?", None, "q3")])


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


def test_deadletter_clears_the_backoff_timer_so_degraded_actually_sleeps(cfg):
    """A dead-lettered ticket must sleep, not respawn on every sweep forever.

    `ops.fail`'s dead-letter branch deliberately sets no new backoff timer, so
    the snapshot arrives at DEGRADED carrying the RequeueScheduled from the
    PREVIOUS backoff -- a timestamp already in the past, since the ticket had
    to wake past it to fail the final time. `is_due` tests that timer above the
    SLEEPING_PHASES gate, so while the STALLED fold arm left it in place,
    T-65's `PHASE_CLASS[DEGRADED] = "sleeping"` was never reached and the
    passive reconciler (which has no `requeue` verb, so it cannot re-arm the
    timer that woke it) got respawned indefinitely.

    This drives the REAL backoff-then-dead-letter path on purpose: every
    dispatcher `_seed` helper sets its phase with a raw PhaseChanged, whose
    fold arm clears the timer, so a test written on one of those cannot
    observe this bug at all.
    """
    home = cfg.home
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING)
    ops.fail(cfg, "T-1", "boom")      # backoff -> appends RequeueScheduled
    ops.fail(cfg, "T-1", "boom")      # backoff -> appends another
    assert snap_mod.load(home, "T-1").next_requeue_at is not None
    assert ops.fail(cfg, "T-1", "boom") == "dead-letter"

    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.DEGRADED.value
    assert snap.next_requeue_at is None

    # Far past any backoff the ticket could have scheduled: sleeping, not "timer".
    verdict = disp.is_due(home, "T-1", snap, inbox_pending=False,
                          current_spec_hash=disp.spec_hash_on_disk(home, "T-1"),
                          now=store.now_epoch() + 86_400)
    assert (verdict.due, verdict.reason) == (False, "sleeping")


def test_a_human_still_revives_a_dead_lettered_ticket(cfg):
    """The clear must not cost the revival path: `inbox_pending` sits above the
    timer branch in `is_due`, so `maestro cmd <KEY> retry` still wakes it."""
    home = cfg.home
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING)
    for _ in range(3):
        ops.fail(cfg, "T-1", "boom")
    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.DEGRADED.value

    inbox.append_command(home, "T-1", "cmd", {"text": "retry"})
    verdict = disp.is_due(home, "T-1", snap, inbox_pending=inbox.has_pending(home, "T-1"),
                          current_spec_hash=disp.spec_hash_on_disk(home, "T-1"),
                          now=store.now_epoch() + 86_400)
    assert (verdict.due, verdict.reason) == (True, "inbox")


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
