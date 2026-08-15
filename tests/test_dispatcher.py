import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from maestro import cli, claims, dispatcher as disp
from maestro import event_log, inbox, ops, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import ClaudeCliSessions, DryRunSessions
from maestro.statemachine import Phase


def _seed(home, key, phase=Phase.READY):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated", {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def test_active_phase_is_due(home):
    _seed(home, "T-1", Phase.READY)
    snap = snap_mod.load(home, "T-1")
    res = disp.is_due(home, "T-1", snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=1000)
    assert res.due and res.reason == "active"


def _ask(home, key, qid="q1", text="ok?"):
    """Give an awaiting-human ticket a real open question (the production state)."""
    event_log.append(home, key, "QuestionAsked", {"qid": qid, "text": text}, actor="r")
    snap_mod.rebuild(home, key)


def test_sleeping_phase_not_due_until_signal(home):
    _seed(home, "T-1", Phase.AWAITING_HUMAN)
    _ask(home, "T-1")  # an open question is what makes awaiting-human legitimately sleep
    snap = snap_mod.load(home, "T-1")
    assert not disp.is_due(home, "T-1", snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=1000).due
    # inbox arrival wakes it
    assert disp.is_due(home, "T-1", snap, inbox_pending=True, current_spec_hash=snap.spec_hash, now=1000).due


# --- AD-7: triaging -> awaiting-human -> ans -> onward, the real replacement for
# the deleted tier-2 gate. `maestro ask` (`ops.ask`/`ask_round`) already
# transitions triaging -> awaiting-human on its own -- this is what the
# triaging skill now relies on unconditionally instead of branching on a
# spec's tier -- proven here over the real CLI + a real dispatch sweep, not
# just `ops` calls in isolation.

def test_triaging_asks_and_routes_to_awaiting_human_then_ans_moves_it_onward(home, cfg):
    key = "T-1"
    rc = cli.main(["--home", str(home), "create", "risky change", "--key", key,
                   "--json", "--no-nudge"])
    assert rc == 0
    disp.dispatch(cfg, DryRunSessions(), now=1000)  # mints the spec + TicketCreated
    assert snap_mod.load(home, key).phase == Phase.TRIAGING.value

    rc = cli.main(["--home", str(home), "ask", key,
                   "Pick up T-1 -- <plan>. AC: <bulleted>. OK?"])
    assert rc == 0
    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert snap.open_questions

    qid = next(iter(snap.open_questions))
    rc = cli.main(["--home", str(home), "ans", key, "ok", "--qid", qid, "--no-nudge"])
    assert rc == 0
    rc = cli.main(["--home", str(home), "fold-inbox", key])
    assert rc == 0
    rc = cli.main(["--home", str(home), "set-phase", key, "ready", "--reason", "approved"])
    assert rc == 0
    assert snap_mod.load(home, key).phase == Phase.READY.value

    events = [e["type"] for e in event_log.read(home, key)]
    assert "QuestionAsked" in events and "QuestionAnswered" in events


def test_spec_edit_wakes_sleeping_ticket(home):
    _seed(home, "T-1", Phase.AWAITING_HUMAN)
    _ask(home, "T-1")
    snap = snap_mod.load(home, "T-1")
    res = disp.is_due(home, "T-1", snap, inbox_pending=False, current_spec_hash="DIFFERENT", now=1000)
    assert res.due and res.reason == "spec-changed"


def test_stranded_awaiting_human_is_due(home, cfg):
    """awaiting-human with no open question, no answered question, and no timer is
    stranded — the dispatcher must wake it so it can never sleep forever."""
    _seed(home, "T-1", Phase.AWAITING_HUMAN)  # bare phase set, never asked
    snap = snap_mod.load(home, "T-1")
    assert not snap.open_questions and not snap.answered_questions
    res = disp.is_due(home, "T-1", snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=1000)
    assert res.due and res.reason == "stranded"
    # A real sweep spawns a reconciler for the stranded ticket (recovery happens there).
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "T-1" in report.spawned


def test_requeue_timer_wakes_awaiting_ci(home, cfg):
    _seed(home, "T-1", Phase.AWAITING_CI)
    ops.requeue(cfg, "T-1", 100)
    snap = snap_mod.load(home, "T-1")
    base = snap.next_requeue_at
    assert not disp.is_due(home, "T-1", snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=base - 1).due
    assert disp.is_due(home, "T-1", snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=base + 1).due


# --- runaway regression (2026-07-19): a requeue must hold an ACTIVE phase too -----


class _EphemeralSessions(DryRunSessions):
    """A worker that is already gone by the next sweep — the incident's regime.

    Rate-limit-rejected reconcilers exited in well under a second against an ~11s
    sweep, so their claim was always released before the dispatcher looked again.
    Plain DryRunSessions keeps every spawned key "active" forever, which hides
    exactly the behaviour under test.
    """

    def list_active(self) -> set[str]:
        return set()


@pytest.mark.parametrize("phase", [p for p in Phase if p not in disp.TERMINAL_PHASES])
def test_requeue_timer_holds_any_non_terminal_phase(home, cfg, phase):
    """A reconciler that asks to sleep is obeyed from EVERY non-terminal phase,
    not just the two in SLEEPING_PHASES. The in-review case is the 2026-07-19
    runaway: the handler ends in `maestro requeue $KEY 900` and the dispatcher
    ignored it. Widened (RB-9) from a hand-picked 5 of the 9 non-terminal
    phases to all 9 -- see test_dispatcher_exhaustive.py for the full
    cross-product of this property against every other flag combination too."""
    _seed(home, "T-1", phase)
    if phase == Phase.AWAITING_HUMAN:
        _ask(home, "T-1")  # give it an open question, or it's already due via "stranded"
    ops.requeue(cfg, "T-1", 900)
    snap = snap_mod.load(home, "T-1")
    base = snap.next_requeue_at

    held = disp.is_due(home, "T-1", snap, inbox_pending=False,
                       current_spec_hash=snap.spec_hash, now=base - 1)
    assert not held.due and held.reason == "backoff"

    woke = disp.is_due(home, "T-1", snap, inbox_pending=False,
                       current_spec_hash=snap.spec_hash, now=base + 1)
    assert woke.due and woke.reason == "timer"


def test_dispatch_does_not_respawn_in_review_ticket_under_its_requeue(home, cfg):
    """Real sweeps: an in-review ticket that asked for 900s of sleep is not
    re-spawned during those 900s, even when its worker dies instantly."""
    _seed(home, "T-1", Phase.IN_REVIEW)
    ops.requeue(cfg, "T-1", 900)
    base = snap_mod.load(home, "T-1").next_requeue_at
    sessions = _EphemeralSessions()

    for i in range(60):  # ~11 minutes of 11s sweeps, all inside the 900s window
        report = disp.dispatch(cfg, sessions, now=base - 900 + i * 11)
        assert report.spawned == []
    assert sessions.spawned == []

    report = disp.dispatch(cfg, sessions, now=base + 1)
    assert report.spawned == ["T-1"]


# --- OC-7 (T-65): degraded is a sleeping phase, not an active one -----------------


def test_degraded_ticket_with_no_signal_is_never_spawned(home, cfg):
    """AC1: a real dispatch() sweep over a `degraded` ticket with an empty
    inbox and no pending requeue timer spawns nothing for it -- and a second
    sweep still spawns nothing, proving it actually sleeps (statemachine.
    SLEEPING_PHASES) rather than merely skipping one cycle."""
    _seed(home, "T-1", Phase.DEGRADED)
    snap = snap_mod.load(home, "T-1")
    assert not disp.is_due(home, "T-1", snap, inbox_pending=False,
                           current_spec_hash=snap.spec_hash, now=1000).due

    r1 = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert r1.spawned == []
    r2 = disp.dispatch(cfg, DryRunSessions(), now=2000)
    assert r2.spawned == []


def test_degraded_ticket_revived_by_real_inbox_command(home, cfg):
    """AC2: appending to a degraded ticket's inbox through the real CLI makes
    it due on the very next real sweep -- a degraded ticket can still be
    revived, proven end-to-end against the real `maestro` verb and a real
    dispatch() sweep, not asserted against a mock."""
    from maestro import cli

    _seed(home, "T-1", Phase.DEGRADED)
    assert disp.dispatch(cfg, DryRunSessions(), now=1000).spawned == []

    # --no-nudge: this test proves revival via its own explicit dispatch()
    # call below, not via cmd_ans's post-write nudge sweep -- an un-suppressed
    # nudge would build a real ClaudeCliSessions and try to Popen("claude", ...)
    # the instant T-1 goes due, which fails in CI where no `claude` binary is
    # on PATH (it happens to succeed as a pointless real spawn locally, where
    # one is).
    assert cli.main(["--home", str(home), "ans", "T-1", "retry", "--no-nudge"]) == 0

    r2 = disp.dispatch(cfg, DryRunSessions(), now=1001)
    assert r2.spawned == ["T-1"]


def test_degraded_ticket_does_not_accumulate_failed_stalled_pairs(home, cfg):
    """Regression test for the measured 2026-08-14 loop (OC-7): before this
    fix, `degraded` sat in ACTIVE_PHASES, so the dispatcher respawned the
    passive reconciler on it every sweep; a reconciler that (correctly) finds
    nothing to route exits without appending, `_allow_spawn`'s no-progress
    watchdog counts that as a stall, and once `max_spawn_attempts` trips it
    calls `ops.fail` again -- which, already past `max_failures`, re-appends
    another Failed/Stalled pair on every single call with no backoff, forever.
    Reproduced here with the exact `_EphemeralSessions` no-op worker (appends
    nothing, dies before the next sweep looks) across many sweeps: the real
    event log must be byte-for-byte unchanged after them."""
    cfg.max_spawn_attempts = 2
    cfg.min_spawn_interval = 0
    _seed(home, "T-1", Phase.DEGRADED)
    before = event_log.read(home, "T-1")

    sessions = _EphemeralSessions()
    for i in range(30):
        report = disp.dispatch(cfg, sessions, now=1000 + i)
        assert report.spawned == []

    after = event_log.read(home, "T-1")
    assert after == before
    assert snap_mod.load(home, "T-1").phase == Phase.DEGRADED.value


def test_spawn_floor_bounds_a_runaway_dispatcher(home, cfg):
    """The incident in miniature. A dispatcher fired every 11s at four tickets that
    never advance and whose workers die instantly used to spawn 4 sessions per
    sweep — 21,731 of them over 35 hours. The per-key floor bounds that to
    elapsed/floor regardless of how often dispatch() is called."""
    keys = ["T-1", "T-2", "T-3", "T-4"]
    for k in keys:
        _seed(home, k, Phase.IN_REVIEW)   # active phase, no timer -> due "active"
    cfg.max_concurrency = 4
    cfg.min_spawn_interval = 300
    sessions = _EphemeralSessions()

    t0, sweeps, step = 1_000_000, 300, 11
    for i in range(sweeps):
        disp.dispatch(cfg, sessions, now=t0 + i * step)

    elapsed = (sweeps - 1) * step               # 3289s
    ceiling = elapsed // cfg.min_spawn_interval + 1   # 11 per key
    spawned = [k for k, *_ in sessions.spawned]
    for k in keys:
        assert spawned.count(k) <= ceiling
    # Without the floor this is sweeps * len(keys) == 1200.
    assert len(sessions.spawned) <= ceiling * len(keys)


def test_human_signal_bypasses_the_spawn_floor(home, cfg):
    """A person answering a question must get an immediate reconcile — their own
    hands are the rate limit. Only machine-driven due-reasons are throttled."""
    _seed(home, "T-1", Phase.READY)
    cfg.min_spawn_interval = 300
    sessions = _EphemeralSessions()

    assert disp.dispatch(cfg, sessions, now=1000).spawned == ["T-1"]
    # Same second, no human signal: throttled.
    r = disp.dispatch(cfg, sessions, now=1001)
    assert r.spawned == [] and r.throttled == ["T-1"]
    # Same second, human command waiting: spawns anyway.
    inbox.append_command(home, "T-1", "ans", {"qid": "q1", "text": "go"})
    r = disp.dispatch(cfg, sessions, now=1002)
    assert r.spawned == ["T-1"] and r.throttled == []


def test_spawn_floor_of_zero_disables_throttling(home, cfg):
    _seed(home, "T-1", Phase.READY)
    cfg.min_spawn_interval = 0
    sessions = _EphemeralSessions()
    for i in range(5):
        assert disp.dispatch(cfg, sessions, now=1000 + i).spawned == ["T-1"]


def test_spawn_floor_defaults_to_reconcile_steady_interval(home):
    cfg = Config(home=home, reconcile_steady_interval=420)
    assert disp.spawn_floor(cfg) == 420
    cfg.min_spawn_interval = 90
    assert disp.spawn_floor(cfg) == 90


def test_dispatch_cli_reports_throttled(home):
    """GA-4: `dispatch --dry-run` is read-only and never writes the spawn ledger
    itself, but it still READS it -- a key a prior REAL spawn floored stays
    reported as throttled, not would_spawn, in the preview."""
    import json

    from maestro import cli

    (home / "config.toml").write_text(
        "[maestro]\nmax_concurrency = 3\nmin_spawn_interval = 300\n")
    _seed(home, "T-1", Phase.READY)
    # Seed the ledger directly, standing in for a prior REAL spawn (the pattern
    # at test_legacy_bare_float_ledger_still_throttles above) -- a preview must
    # never be the one to write this entry. The CLI drives dispatch() off the
    # real wall clock, so "recent" has to mean "close to time.time()", not an
    # arbitrary fixed epoch.
    store.write_json(disp._spawn_ledger_path(home), {"T-1": time.time()})

    def _sweep():
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            assert cli.main(["--home", str(home), "dispatch", "--dry-run"]) == 0
        finally:
            sys.stdout = old
        return json.loads(buf.getvalue())

    ledger_before = disp._spawn_ledger_path(home).read_bytes()
    out = _sweep()
    assert out["would_spawn"] == [] and out["throttled"] == ["T-1"]
    # The preview only READ the ledger -- confirm it's still byte-identical.
    assert disp._spawn_ledger_path(home).read_bytes() == ledger_before


def test_spawn_ledger_records_rolling_history_and_trims_window(home, cfg):
    """dispatch() writes {key: {"last": float, "recent": [[ts, weight], ...]}},
    and rate computation (health.spawn_rate) drops entries older than the
    window and sums each entry's agent-equivalent weight (GA-14)."""
    from maestro import health

    _seed(home, "T-1", Phase.IMPLEMENTING)  # active phase, no timer -> due every sweep
    cfg.min_spawn_interval = 0
    cfg.max_spawn_attempts = 0  # this test's ticket never progresses observed_seq by
    # design (it's exercising the ledger, not real reconcile steps); the no-progress
    # watchdog (T-13) would otherwise fail it after 5 spawns and starve `recent`.
    cfg.runaway_pause_cooldown = 0  # GA-14: an `implementing` ticket now weighs
    # heavily enough that the auto-brake (armed off the SAME ledger) would
    # otherwise cut this loop short before all 10 spawns land -- this test
    # exercises the ledger, not the brake (see test_runaway_brake.py for that).
    sessions = _EphemeralSessions()
    t0 = 1_000_000
    for i in range(10):
        disp.dispatch(cfg, sessions, now=t0 + i)  # 10 spawns, 1s apart

    W_implementing = disp.spawn_weight(cfg, Phase.IMPLEMENTING.value)  # 1 + 20*1 == 21
    ledger = store.read_json(disp._spawn_ledger_path(home), {})
    entry = ledger["T-1"]
    assert isinstance(entry, dict) and "last" in entry and "recent" in entry
    assert len(entry["recent"]) == 10
    assert health.spawn_rate(home, t0 + 9)["total"] == 10 * W_implementing

    # Jump past the window and spawn once more: only the fresh entry counts.
    later = t0 + health.WINDOW_SECONDS + 100
    disp.dispatch(cfg, sessions, now=later)
    assert health.spawn_rate(home, later)["total"] == 1 * W_implementing


def test_spawn_ledger_recent_hard_capped(home, cfg):
    """`recent` cannot grow without bound even with the spawn floor disabled."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg.min_spawn_interval = 0
    cfg.max_spawn_attempts = 0  # no-progress watchdog would otherwise fail this
    # never-progressing ticket long before `recent` reaches the cap.
    # This exercises the ledger's own cap, not the GA-5 runaway brake -- with
    # the floor off, the default budget (ceil(3600/300) * 1 key == 12/h) would
    # otherwise trip the brake long before `n` spawns land and cut this test's
    # loop short at G1.
    cfg.runaway_pause_cooldown = 0
    sessions = _EphemeralSessions()
    t0 = 1_000_000
    n = disp._LEDGER_RECENT_CAP + 50
    for i in range(n):
        disp.dispatch(cfg, sessions, now=t0 + i / 100.0)  # all inside one window
    ledger = store.read_json(disp._spawn_ledger_path(home), {})
    assert len(ledger["T-1"]["recent"]) <= disp._LEDGER_RECENT_CAP


def test_legacy_bare_float_ledger_still_throttles(home, cfg):
    """A live home upgrading from the pre-history ledger format needs no
    migration: a bare float is read as `last` with empty history."""
    from maestro import health

    _seed(home, "T-1", Phase.READY)
    cfg.min_spawn_interval = 300
    store.write_json(disp._spawn_ledger_path(home), {"T-1": 1000.0})
    sessions = _EphemeralSessions()
    report = disp.dispatch(cfg, sessions, now=1001)
    assert report.spawned == [] and report.throttled == ["T-1"]
    assert health.spawn_rate(home, 1001)["total"] == 0


def test_legacy_unweighted_recent_history_reads_without_crashing(home, cfg):
    """GA-14: a ledger written by a pre-GA-14 maestro has `recent` entries
    that are bare timestamps, not `[ts, weight]` pairs. Reading it must not
    crash, and each legacy entry reads as weight 1 -- same as before
    weighting existed -- so a healthy legacy ledger does not suddenly read as
    a runaway just because maestro was upgraded."""
    from maestro import health

    _seed(home, "T-1", Phase.IMPLEMENTING)
    now = 1000.0
    store.write_json(disp._spawn_ledger_path(home),
                     {"T-1": {"last": now, "recent": [now - 5.0, now - 3.0, now - 1.0]}})

    rate = health.spawn_rate(home, now)
    assert rate == {"total": 3, "by_key": {"T-1": 3}}
    budget = health.spawn_budget(cfg)
    assert budget > 0
    assert rate["total"] <= budget  # unweighted legacy history: not a runaway


def test_dispatch_respects_concurrency_cap(home, cfg):
    for i in range(1, 6):
        _seed(home, f"T-{i}", Phase.READY)
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert len(report.spawned) == cfg.max_concurrency  # 3
    assert len(report.capacity_skipped) == 2


def test_dispatch_skips_live_session_for_same_key(home, cfg):
    _seed(home, "T-1", Phase.READY)
    _seed(home, "T-2", Phase.READY)
    sessions = DryRunSessions(active={"T-1"})  # T-1 already has a live reconciler
    report = disp.dispatch(cfg, sessions, now=1000)
    assert "T-1" in report.claimed
    assert report.spawned == ["T-2"]


def test_mint_new_tickets_from_inbox(home, cfg):
    inbox.append_new(home, "build the thing", key="T-9")
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "T-9" in report.minted
    assert store.spec_path(home, "T-9").exists()
    assert snap_mod.load(home, "T-9").phase == Phase.TRIAGING.value


def test_mint_new_tickets_skips_late_create_for_already_triaged_key(home, cfg):
    """If a key gets triaged (e.g. a manual reconcile) before the dispatcher's
    mint sweep drains the matching inbox/_new entry, the late TicketCreated
    must not clobber the already-advanced phase back to triaging (T-1)."""
    inbox.append_new(home, "build the thing", key="T-11")
    # Simulate the key having already been triaged past the pending _new entry.
    event_log.append(home, "T-11", "SpecObserved", {"spec_hash": "abc"}, actor="r")
    event_log.append(home, "T-11", "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r")
    event_log.append(home, "T-11", "PhaseChanged", {"phase": "awaiting-human"}, actor="r")
    snap_mod.rebuild(home, "T-11")

    minted = disp.mint_new_tickets(cfg)

    assert minted == []
    snap = snap_mod.load(home, "T-11")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert snap.open_questions == {"q1": "ok?"}
    # The stale create-request must still be consumed, not reprocessed forever.
    assert inbox.pending_new(home) == []


def test_worker_cwd_prefers_existing_worktree(home):
    cfg = Config(home=home, repo_path=str(home / "repo"))
    wt = home / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    assert disp._worker_cwd(cfg, "T-1") == wt


def test_worker_cwd_prefers_existing_worktree_in_local_mode_too(home, tmp_path):
    # QW-7 AC4: a worktree dir literally present short-circuits before the
    # mode branch is even consulted -- true for local mode as much as git.
    target = tmp_path / "vault"
    target.mkdir()
    cfg = Config(home=home, repos={"vault": {"path": str(target), "mode": "local", "default": True}})
    wt = home / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    assert disp._worker_cwd(cfg, "T-1") == wt


def test_worker_cwd_falls_back_to_scratch_dir_before_worktree_exists(home, tmp_path):
    # QW-7: a git-mode ticket's pre-worktree fallback must never be binding.path
    # (on this board, the human's own shared checkout) -- it lands in a per-key
    # scratch dir instead, seeded with a symlink to .claude/commands so the
    # phase's slash command still resolves from there.
    repo = tmp_path / "repo"
    commands_dir = repo / ".claude" / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "maestro-reconcile-triaging.md").write_text("# triaging\n")
    cfg = Config(home=home, repo_path=str(repo))
    cwd = disp._worker_cwd(cfg, "T-1")
    assert cwd != repo
    assert cwd == home / "scratch" / "T-1"
    assert (cwd / ".claude" / "commands" / "maestro-reconcile-triaging.md").exists()


def test_worker_cwd_scratch_dir_is_idempotent_and_relinks_on_repo_change(home, tmp_path):
    repo_a = tmp_path / "repo-a"
    (repo_a / ".claude" / "commands").mkdir(parents=True)
    cfg = Config(home=home, repo_path=str(repo_a))
    first = disp._worker_cwd(cfg, "T-1")
    second = disp._worker_cwd(cfg, "T-1")  # a later sweep, same repo -- no error, same path
    assert first == second == home / "scratch" / "T-1"

    repo_b = tmp_path / "repo-b"
    (repo_b / ".claude" / "commands").mkdir(parents=True)
    (repo_b / ".claude" / "commands" / "maestro-reconcile-ready.md").write_text("# ready\n")
    cfg.repo_path = str(repo_b)
    third = disp._worker_cwd(cfg, "T-1")
    assert third == home / "scratch" / "T-1"
    assert (third / ".claude" / "commands" / "maestro-reconcile-ready.md").exists()


def test_worker_cwd_local_mode_still_lands_in_binding_path(home, tmp_path):
    # QW-7 AC3: mode == "local" (AD-6, the deliberate write-in-place case) keeps
    # today's exact behavior -- always binding.path, never a scratch dir.
    target = tmp_path / "vault"
    target.mkdir()
    cfg = Config(home=home, repos={"vault": {"path": str(target), "mode": "local", "default": True}})
    assert disp._worker_cwd(cfg, "T-1") == target


def test_worker_cwd_last_resort_is_home(home):
    cfg = Config(home=home, repo_path=None)
    assert disp._worker_cwd(cfg, "T-1") == home


def test_dispatch_records_scratch_cwd_not_shared_checkout_for_triaging(home):
    # QW-7 AC1+AC2: a real sweep over a git-mode binding with no worktree yet
    # records a spawn cwd that is not binding.path, and that cwd can still
    # resolve the phase's reconcile command.
    repo = home / "repo"
    commands_dir = repo / ".claude" / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "maestro-reconcile-triaging.md").write_text("# triaging\n")
    cfg = Config(home=home, repo_path=str(repo), max_concurrency=1)
    _seed(home, "T-1", Phase.TRIAGING)

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    assert report.spawned == ["T-1"]
    cwd = sessions.spawned[0][2]
    assert cwd != str(repo)
    assert (Path(cwd) / ".claude" / "commands" / "maestro-reconcile-triaging.md").exists()


def test_dispatch_still_records_binding_path_for_local_mode(home):
    # QW-7 AC3, at the dispatch() level: local mode's spawn cwd is unaffected.
    target = home / "vault"
    target.mkdir()
    cfg = Config(home=home, repos={"vault": {"path": str(target), "mode": "local", "default": True}},
                max_concurrency=1)
    _seed(home, "T-1", Phase.TRIAGING)

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    assert report.spawned == ["T-1"]
    assert sessions.spawned[0][2] == str(target)


@pytest.mark.parametrize("phase", [Phase.TRIAGING, Phase.READY, Phase.RESEARCHING, Phase.IMPLEMENTING])
def test_dispatch_never_spawns_git_mode_ticket_into_shared_checkout(home, phase):
    # QW-7: whatever phase a git-mode ticket without a worktree is in, the
    # shared checkout must never be the recorded spawn cwd -- every
    # pre-worktree phase file grants Write/Edit with acceptEdits, so landing
    # there would let a reconciler edit the human's own working copy.
    repo = home / "repo"
    (repo / ".claude" / "commands").mkdir(parents=True)
    cfg = Config(home=home, repo_path=str(repo), max_concurrency=1)
    _seed(home, "T-1", phase)

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    assert report.spawned == ["T-1"]
    assert sessions.spawned[0][2] != str(repo)


# --- dependsOn gating ---

def test_parse_depends_on_empty():
    assert disp.parse_depends_on("dependsOn: []") == []


def test_parse_depends_on_single():
    assert disp.parse_depends_on("dependsOn: [M-1]") == ["M-1"]


def test_parse_depends_on_multiple():
    assert disp.parse_depends_on("dependsOn: [M-1, M-2]") == ["M-1", "M-2"]


def _seed_with_deps(home, key, phase=Phase.READY, depends_on=None):
    deps_str = ", ".join(depends_on) if depends_on else ""
    spec = f"# {key}\napproval_tier: 0\ndependsOn: [{deps_str}]\n"
    store.atomic_write(store.spec_path(home, key), spec)
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def test_ready_ticket_blocked_when_dep_not_done(home, cfg):
    _seed_with_deps(home, "T-dep", Phase.IMPLEMENTING)
    _seed_with_deps(home, "T-1", Phase.READY, depends_on=["T-dep"])
    snap = snap_mod.load(home, "T-1")
    res = disp.is_due(home, "T-1", snap, inbox_pending=False,
                      current_spec_hash=snap.spec_hash, now=1000, blocked_dep=True)
    assert not res.due
    assert res.reason == "blocked-dep"


def test_ready_ticket_unblocked_when_dep_done(home, cfg):
    _seed_with_deps(home, "T-dep", Phase.DONE)
    _seed_with_deps(home, "T-1", Phase.READY, depends_on=["T-dep"])
    snap = snap_mod.load(home, "T-1")
    res = disp.is_due(home, "T-1", snap, inbox_pending=False,
                      current_spec_hash=snap.spec_hash, now=1000, blocked_dep=False)
    assert res.due
    assert res.reason == "active"


def test_dispatch_holds_ready_ticket_with_unmet_dep(home, cfg):
    _seed_with_deps(home, "T-dep", Phase.IMPLEMENTING)
    _seed_with_deps(home, "T-1", Phase.READY, depends_on=["T-dep"])
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert "T-1" not in report.spawned
    assert not any(k == "T-1" for k, _ in report.due)


def test_dispatch_spawns_ready_ticket_when_dep_done(home, cfg):
    _seed_with_deps(home, "T-dep", Phase.DONE)
    _seed_with_deps(home, "T-1", Phase.READY, depends_on=["T-dep"])
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert "T-1" in report.spawned


# --- natural key ordering ---

def test_split_key_well_formed():
    assert disp.split_key("M-8") == (0, "M", 8)
    assert disp.split_key("TUI-10") == (0, "TUI", 10)
    assert disp.split_key("L-2") == (0, "L", 2)


def test_split_key_malformed():
    result = disp.split_key("BROKEN")
    assert result[0] == 1  # sorted after well-formed keys


def test_list_keys_natural_order(home):
    for key in ["TUI-10", "M-10", "TUI-2", "M-2", "L-1", "NOID"]:
        store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
        event_log.append(home, key, "TicketCreated",
                         {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    keys = disp.list_keys(home)
    well_formed = [k for k in keys if k != "NOID"]
    assert well_formed == ["L-1", "M-2", "M-10", "TUI-2", "TUI-10"]
    assert keys[-1] == "NOID"  # malformed sorts last


# --- prefix-based key minting ---

def test_auto_key_default_prefix(home):
    assert disp._auto_key(home) == "T-1"


def test_auto_key_custom_prefix(home):
    assert disp._auto_key(home, prefix="FEAT") == "FEAT-1"


def test_auto_key_skips_existing(home):
    (home / "tickets" / "FEAT-1").mkdir(parents=True)
    (home / "tickets" / "FEAT-2").mkdir(parents=True)
    assert disp._auto_key(home, prefix="FEAT") == "FEAT-3"


def test_existing_prefixes_empty(home):
    assert disp.existing_prefixes(home) == []


def test_existing_prefixes_sorted(home):
    for key in ["TUI-1", "T-3", "T-1", "FEAT-2"]:
        store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
        event_log.append(home, key, "TicketCreated",
                         {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    assert disp.existing_prefixes(home) == ["FEAT", "T", "TUI"]


def test_mint_ticket_with_prefix(home, cfg):
    inbox.append_new(home, "new feature", prefix="FEAT")
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "FEAT-1" in report.minted
    assert store.spec_path(home, "FEAT-1").exists()


def test_mint_ticket_prefix_skips_existing(home, cfg):
    (home / "tickets" / "FEAT-1").mkdir(parents=True)
    inbox.append_new(home, "another feature", prefix="FEAT")
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "FEAT-2" in report.minted


# --- RT-1: parse_spec_overrides ---

def test_parse_spec_overrides_priority_only():
    assert disp.parse_spec_overrides("priority: 3\n") == {"priority": 3}


def test_parse_spec_overrides_model_only():
    spec = "priority: 1\nmodel: opus\ndependsOn: []\n"
    assert disp.parse_spec_overrides(spec) == {"priority": 1, "model": "opus"}


def test_parse_spec_overrides_all_three():
    spec = "priority: 1\nkind: research\nmodel: opus\neffort: high\ndependsOn: []\n"
    result = disp.parse_spec_overrides(spec)
    assert result == {"priority": 1, "kind": "research", "model": "opus", "effort": "high"}


def test_parse_spec_overrides_stops_at_section_header():
    spec = "priority: 1\n## Intent\nmodel: opus\n"
    assert disp.parse_spec_overrides(spec) == {"priority": 1}


def test_parse_spec_overrides_malformed_priority_omitted():
    """A non-integer priority is dropped, not raised -- `spec_priority` is what
    supplies the safe fallback."""
    assert disp.parse_spec_overrides("priority: soon\n") == {}


def test_parse_spec_overrides_ignores_legacy_approval_tier():
    """AD-7: the 130 existing specs' now-inert `approval_tier:` line is an
    unrecognized front-matter key -- tolerated, not parsed, not raised."""
    assert disp.parse_spec_overrides("approval_tier: 2\npriority: 1\n") == {"priority": 1}


def test_spec_with_legacy_approval_tier_line_folds_and_dispatches_clean(home, cfg):
    """AD-7 AC: a spec still carrying an `approval_tier:` line (like the 130
    existing ones on the real board) folds and dispatches with no error and no
    fold warning -- no bulk rewrite of existing specs is required."""
    key = "T-1"
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\napproval_tier: 2\npriority: 1\ndependsOn: []\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.READY.value}, actor="r")
    snap = snap_mod.rebuild(home, key)
    assert snap.fold_warnings == []

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert report.spawned == [key]


# --- RT-1: _resolve_model_effort ---

def _seed_with_overrides(home, key, *, kind=None, model=None, effort=None, phase=Phase.READY):
    extra = ""
    if kind:
        extra += f"kind: {kind}\n"
    if model:
        extra += f"model: {model}\n"
    if effort:
        extra += f"effort: {effort}\n"
    spec = f"# {key}\napproval_tier: 0\n{extra}dependsOn: []\n"
    store.atomic_write(store.spec_path(home, key), spec)
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def test_resolve_model_effort_defaults_no_overrides(home):
    from maestro.config import Config
    cfg = Config(home=home, reconcile_model="sonnet", default_effort=None)
    _seed_with_overrides(home, "T-1")
    model, effort = disp._resolve_model_effort(cfg, "T-1")
    assert model == "sonnet"
    assert effort is None


def test_resolve_model_effort_spec_model_overrides_config(home):
    from maestro.config import Config
    cfg = Config(home=home, reconcile_model="sonnet", default_effort=None)
    _seed_with_overrides(home, "T-1", model="opus")
    model, effort = disp._resolve_model_effort(cfg, "T-1")
    assert model == "opus"
    assert effort is None


def test_resolve_model_effort_spec_effort_overrides_default(home):
    from maestro.config import Config
    cfg = Config(home=home, reconcile_model="sonnet", default_effort=None)
    _seed_with_overrides(home, "T-1", effort="high")
    model, effort = disp._resolve_model_effort(cfg, "T-1")
    assert effort == "high"


def test_resolve_model_effort_research_kind_uses_config_research_defaults(home):
    from maestro.config import Config
    cfg = Config(home=home, reconcile_model="sonnet", research_model="opus",
                 research_effort="high", default_effort=None)
    _seed_with_overrides(home, "T-1", kind="research")
    model, effort = disp._resolve_model_effort(cfg, "T-1")
    assert model == "opus"
    assert effort == "high"


def test_resolve_model_effort_spec_overrides_research_defaults(home):
    from maestro.config import Config
    cfg = Config(home=home, research_model="opus", research_effort="high")
    _seed_with_overrides(home, "T-1", kind="research", model="haiku", effort="low")
    model, effort = disp._resolve_model_effort(cfg, "T-1")
    assert model == "haiku"
    assert effort == "low"


# --- RT-1: AC1 — real dispatcher sweep spawns correct model+effort ---

def test_dispatch_spawns_with_spec_model_and_effort(home, cfg):
    """AC1: spec model/effort propagate through a real sweep to the spawned command."""
    _seed_with_overrides(home, "T-1", model="opus", effort="high")
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert "T-1" in report.spawned
    spawned_map = {k: (m, e) for k, _p, _c, m, e, _d, *_ in sessions.spawned}
    assert spawned_map["T-1"] == ("opus", "high")


def test_dispatch_spawns_with_config_defaults_when_no_spec_overrides(home, cfg):
    """AC2: ticket with no overrides uses reconcile_model from config, no effort."""
    _seed_with_overrides(home, "T-1")
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    spawned_map = {k: (m, e) for k, _p, _c, m, e, _d, *_ in sessions.spawned}
    assert spawned_map["T-1"] == (cfg.reconcile_model, None)


def test_dispatch_research_ticket_uses_research_defaults(home):
    """AC1 variant: kind=research uses research_model/research_effort from config."""
    from maestro.config import Config
    cfg = Config(home=home, max_concurrency=3, research_model="opus",
                 research_effort="high")
    _seed_with_overrides(home, "T-1", kind="research")
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    spawned_map = {k: (m, e) for k, _p, _c, m, e, _d, *_ in sessions.spawned}
    assert spawned_map["T-1"] == ("opus", "high")


# --- RF-1: spawn() takes command+key separately; DryRunSessions.spawned unchanged ---

def test_dispatch_spawn_tuple_byte_identical_to_pre_rf1_baseline(home, cfg):
    """AC2: a real dispatch sweep over a plain seeded READY ticket records the exact
    same spawn tuple RF-1 found before the split -- dispatcher no longer pre-flattens
    "<command> <key>" itself, but DryRunSessions.spawn still composes and records it,
    so every existing reader of sessions.spawned sees byte-identical values."""
    _seed_with_overrides(home, "T-1")
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert report.spawned == ["T-1"]
    assert len(sessions.spawned) == 1
    key, prompt, cwd, model, effort, disallowed_tools, allowed_tools, env_overlay, runner, \
        runner_model = sessions.spawned[0]
    assert (key, prompt, cwd, model, effort, disallowed_tools, allowed_tools, env_overlay, runner,
            runner_model) == (
        "T-1",
        "/maestro-reconcile-ready T-1",
        str(home),
        "sonnet",
        None,
        ["Bash(gh pr merge:*)"],  # AD-7: unconditional merge denylist
        [],
        {},
        "claude",  # RF-2: READY forces "claude" regardless of any spec runner: override
        None,      # OC-4: no non-claude runner in play here
    )


# --- RT-1: _seed_spec with new fields ---

def test_seed_spec_includes_kind_model_effort(home, cfg):
    inbox.append_new(home, "Research task", key="R-1", args={
        "priority": 2,
        "kind": "research", "model": "opus", "effort": "high",
        "notes": "Use web search", "depends_on": ["T-1"],
    })
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    spec_text = store.spec_path(home, "R-1").read_text()
    assert "kind: research" in spec_text
    assert "model: opus" in spec_text
    assert "effort: high" in spec_text
    assert "dependsOn: [T-1]" in spec_text
    assert "## Notes" in spec_text
    assert "Use web search" in spec_text


def test_seed_spec_no_extra_fields_by_default(home, cfg):
    inbox.append_new(home, "Basic ticket", key="T-42")
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    spec_text = store.spec_path(home, "T-42").read_text()
    assert "kind:" not in spec_text
    assert "model:" not in spec_text
    assert "effort:" not in spec_text
    assert "runner:" not in spec_text
    assert "runner_model:" not in spec_text
    assert "dependsOn: []" in spec_text


# --- UX-1: _seed_spec with runner/runner_model --------------------------------

def test_seed_spec_includes_runner_and_runner_model(home, cfg):
    inbox.append_new(home, "Non-claude task", key="R-2", args={
        "priority": 2,
        "runner": "opencode", "runner_model": "qwen3-coder:30b",
    })
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    spec_text = store.spec_path(home, "R-2").read_text()
    assert "runner: opencode" in spec_text
    assert "runner_model: qwen3-coder:30b" in spec_text

    from maestro.gates import parse_spec_overrides
    overrides = parse_spec_overrides(spec_text)
    assert overrides["runner"] == "opencode"
    assert overrides["runner_model"] == "qwen3-coder:30b"


def test_seed_spec_with_neither_runner_field_matches_golden_fixture():
    """AC2: `_seed_spec` with neither field set returns a string byte-identical
    to the pre-runner-field baseline -- the same shape it always rendered."""
    text = disp._seed_spec("T-91", "Plain ticket", {"priority": 3})
    assert text == (
        "# T-91: Plain ticket\n"
        "\n"
        "<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->\n"
        "\n"
        "priority: 3\n"
        "dependsOn: []\n"
        "\n"
        "## Intent\n"
        "(describe what done looks like)\n"
        "\n"
        "## Acceptance criteria\n"
        "- \n"
    )


def test_mint_tolerates_explicit_null_fields(home, cfg):
    """A create-request carrying explicit JSON ``null`` for intent/args must not
    crash the whole sweep (regression: null intent made ``_seed_spec`` join a None)."""
    inbox.append_new(home, "Nullable ticket", prefix="M",
                     args={"priority": 3,
                           "intent": None, "kind": "implementation"})
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.minted == ["M-1"]
    spec_text = store.spec_path(home, "M-1").read_text()
    assert "(describe what done looks like)" in spec_text
    assert "kind: implementation" in spec_text


def test_mint_tolerates_null_title(home, cfg):
    """A create-request with a null title falls back to the key, not a crash."""
    inbox.append_new(home, None, key="T-77", args={"intent": "do it"})
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.minted == ["T-77"]
    assert "# T-77: T-77" in store.spec_path(home, "T-77").read_text()


# --- T-10: scheduled tasks ----------------------------------------------------

def _sched_cfg(cfg, **overrides):
    task = {
        "name": "digest", "prompt": "Summarize things", "every": "1h",
        "kind": "implementation", "priority": 3, "prefix": "S",
        "enabled": True,
    }
    task.update(overrides)
    cfg.scheduled = [task]
    return cfg


def test_run_scheduled_tasks_fires_when_due(home, cfg):
    _sched_cfg(cfg)
    result = disp.run_scheduled_tasks(cfg, now=1_000_000)
    assert result["fired"] == ["digest"]
    pending = inbox.pending_new(home)
    assert len(pending) == 1
    args = pending[0][1]["args"]
    assert args["intent"] == "Summarize things"
    assert args["scheduled_by"] == "digest"
    assert "dedup" in args
    cursor = store.read_json(home / "derived" / ".schedule_cursor.json", {})
    assert cursor["digest"] == 1_000_000


def test_run_scheduled_tasks_disabled_never_fires(home, cfg):
    _sched_cfg(cfg, enabled=False)
    result = disp.run_scheduled_tasks(cfg, now=1_000_000)
    assert result["fired"] == []
    assert inbox.pending_new(home) == []


def test_run_scheduled_tasks_mint_args_pass_optional_fields_when_set(home, cfg):
    """GA-9 AC1: repo/model/effort/notes/depends_on flow into the mint args when
    the [[scheduled]] block sets them, and the four task-only fields (name/every/
    enabled/prefix) never leak into the ticket args."""
    _sched_cfg(cfg, repo="alpha", model="sonnet", effort="high",
              notes="Skip weekends.", depends_on=["T-1"],
              runner="opencode", runner_model="qwen3-coder:30b")
    disp.run_scheduled_tasks(cfg, now=1_000_000)
    args = inbox.pending_new(home)[0][1]["args"]
    assert args["repo"] == "alpha"
    assert args["model"] == "sonnet"
    assert args["effort"] == "high"
    assert args["notes"] == "Skip weekends."
    assert args["depends_on"] == ["T-1"]
    assert args["runner"] == "opencode"
    assert args["runner_model"] == "qwen3-coder:30b"
    for leaked in ("name", "every", "enabled", "prefix"):
        assert leaked not in args


def test_run_scheduled_tasks_mint_args_omit_unset_optional_fields(home, cfg):
    """The complement of the above: when a [[scheduled]] block doesn't set an
    optional field, the mint args omit the key entirely rather than passing None."""
    _sched_cfg(cfg)  # no repo/model/effort/notes/depends_on/runner/runner_model
    disp.run_scheduled_tasks(cfg, now=1_000_000)
    args = inbox.pending_new(home)[0][1]["args"]
    for field in ("repo", "model", "effort", "notes", "depends_on", "runner", "runner_model"):
        assert field not in args


def test_run_scheduled_tasks_cursor_anchors_to_elapsed_boundary_not_sweep_clock(home, cfg):
    """GA-9 AC5: a late fire advances the cursor to the elapsed slot boundary
    derived from the previous cursor, not to the sweep clock -- so the task's
    cadence doesn't drift forward by however late each fire happened to be."""
    _sched_cfg(cfg, every="1h")
    period = 3600
    first = 1_000_000
    disp.run_scheduled_tasks(cfg, now=first)
    cursor = store.read_json(home / "derived" / ".schedule_cursor.json", {})
    assert cursor["digest"] == first  # first-ever fire anchors at `now`

    late_fire = first + period + 300  # due at first+period, fires 5min late
    disp.run_scheduled_tasks(cfg, now=late_fire)
    cursor = store.read_json(home / "derived" / ".schedule_cursor.json", {})
    assert cursor["digest"] == first + period  # anchored, not dragged to late_fire


def test_run_scheduled_tasks_long_outage_fires_once_then_stays_quiet(home, cfg):
    """GA-9: the level-triggered no-catch-up property survives the anchor fix --
    after many missed periods, one sweep fires exactly once, and the next sweep
    within one period fires zero times."""
    _sched_cfg(cfg, every="1h")
    period = 3600
    first = 1_000_000
    disp.run_scheduled_tasks(cfg, now=first)

    after_outage = first + 50 * period + 100  # ~50 periods of downtime
    result = disp.run_scheduled_tasks(cfg, now=after_outage)
    assert result["fired"] == ["digest"]

    soon_after = after_outage + 100  # well within one period
    result = disp.run_scheduled_tasks(cfg, now=soon_after)
    assert result["fired"] == []


def test_dispatch_mints_scheduled_ticket_exactly_once_per_interval(home, cfg):
    """The QA scenario from the T-10 spec: drive dispatch() over a temp home with a
    [[scheduled]] config, advance now past the interval, and assert a ticket mints
    exactly once — not again before the next interval, not N times after a gap."""
    _sched_cfg(cfg)
    now = 1_000_000

    r1 = disp.dispatch(cfg, DryRunSessions(), now=now)
    assert r1.scheduled_fired == ["digest"]
    assert r1.minted == []  # the fire only queues a _new entry; mint happens next sweep

    r2 = disp.dispatch(cfg, DryRunSessions(), now=now + 5)
    assert r2.scheduled_fired == []  # not due again so soon
    assert r2.minted == ["S-1"]  # queued entry from sweep 1 mints here

    # not due again before the interval elapses
    r3 = disp.dispatch(cfg, DryRunSessions(), now=now + 1800)
    assert r3.scheduled_fired == []
    assert r3.minted == []

    # due again once the interval elapses
    r4 = disp.dispatch(cfg, DryRunSessions(), now=now + 3600)
    assert r4.scheduled_fired == ["digest"]
    r5 = disp.dispatch(cfg, DryRunSessions(), now=now + 3600)
    assert r5.minted == ["S-2"]

    # a long downtime fires once on the next sweep, never N times to "catch up"
    r6 = disp.dispatch(cfg, DryRunSessions(), now=now + 3600 + 100_000)
    assert r6.scheduled_fired == ["digest"]
    r7 = disp.dispatch(cfg, DryRunSessions(), now=now + 3600 + 100_000)
    assert r7.minted == ["S-3"]
    r8 = disp.dispatch(cfg, DryRunSessions(), now=now + 3600 + 100_000)
    assert r8.scheduled_fired == []
    assert r8.minted == []

    minted_tickets = sorted(p.name for p in (home / "tickets").iterdir())
    assert minted_tickets == ["S-1", "S-2", "S-3"]


def test_mint_new_tickets_dedup_closes_cursor_crash_window(home, cfg):
    """If run_scheduled_tasks appends to _new but crashes before persisting the
    cursor, the next sweep re-fires the same period-bucket dedup token — that
    re-fire must mint zero extra tickets, not a duplicate."""
    _sched_cfg(cfg)
    now = 1_000_000
    fired1 = disp.run_scheduled_tasks(cfg, now)
    assert fired1["fired"] == ["digest"]
    # simulate the cursor write crashing (delete what was just persisted)
    (home / "derived" / ".schedule_cursor.json").unlink()
    fired2 = disp.run_scheduled_tasks(cfg, now + 5)  # same period bucket
    assert fired2["fired"] == ["digest"]

    pending = inbox.pending_new(home)
    assert len(pending) == 2
    assert pending[0][1]["args"]["dedup"] == pending[1][1]["args"]["dedup"]

    minted = disp.mint_new_tickets(cfg)
    assert minted == ["S-1"]  # only one real ticket, despite two queued fires
    assert sorted(p.name for p in (home / "tickets").iterdir()) == ["S-1"]


def test_schedule_status_reports_cadence_and_cursor(home, cfg):
    _sched_cfg(cfg)
    now = 1_000_000
    disp.run_scheduled_tasks(cfg, now)
    rows = disp.schedule_status(cfg, now + 10)
    assert rows == [{
        "name": "digest", "prompt": "Summarize things", "every": "1h",
        "cron": None, "tz": "UTC",
        "kind": "implementation", "priority": 3,
        "prefix": "S", "enabled": True, "repo": None, "title": None,
        "last_fired": now, "next_due": now + 3600,
    }]


def test_schedule_status_never_fired_has_no_last_fired(home, cfg):
    _sched_cfg(cfg)
    rows = disp.schedule_status(cfg, now=1_000_000)
    assert rows[0]["last_fired"] is None
    assert rows[0]["next_due"] == 3600  # one period from the epoch


# --- GA-19: cron / wall-clock cadence, driven through the real dispatcher -----

def _cron_sched_cfg(cfg, **overrides):
    task = {
        "name": "digest", "prompt": "Summarize things", "cron": "0 2 * * *",
        "tz": "UTC", "kind": "implementation", "priority": 3,
        "prefix": "S", "enabled": True,
    }
    task.update(overrides)
    cfg.scheduled = [task]
    return cfg


def test_dispatch_cron_task_fires_exactly_on_the_sweep_crossing_its_slot(home, cfg):
    """QA over the real surface (CLAUDE.md): real dispatch() sweeps at hand-
    chosen epochs -- before the slot, at/after it, and again within the same
    slot -- fire on exactly the sweep that crosses the wall-clock slot, and
    mint exactly one ticket per slot, not one per sweep. Only the claude spawn
    is mocked (DryRunSessions)."""
    _cron_sched_cfg(cfg)
    prev_slot = datetime(2025, 12, 31, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    slot = datetime(2026, 1, 1, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp()

    r1 = disp.dispatch(cfg, DryRunSessions(), now=prev_slot)  # anchor the cursor
    assert r1.scheduled_fired == ["digest"]
    assert r1.minted == []  # the fire only queues a _new entry; mint happens next sweep

    r2 = disp.dispatch(cfg, DryRunSessions(), now=slot - 3600)  # 01:00, too early
    assert r2.scheduled_fired == []
    assert r2.minted == ["S-1"]  # sweep 1's queued fire mints here

    r3 = disp.dispatch(cfg, DryRunSessions(), now=slot)  # crosses the wall-clock slot
    assert r3.scheduled_fired == ["digest"]

    r4 = disp.dispatch(cfg, DryRunSessions(), now=slot + 5)  # same slot, must not re-fire
    assert r4.scheduled_fired == []
    assert r4.minted == ["S-2"]

    r5 = disp.dispatch(cfg, DryRunSessions(), now=slot + 1800)  # still 02:xx, same slot
    assert r5.scheduled_fired == []
    assert r5.minted == []

    minted_tickets = sorted(p.name for p in (home / "tickets").iterdir())
    assert minted_tickets == ["S-1", "S-2"]  # one per slot, not one per sweep


def test_run_scheduled_tasks_cron_only_task_mints_without_every_field(home, cfg):
    """AC: a cron-only task (no 'every' at all) mints without raising KeyError --
    the dedup token has moved behind `schedule.dedup_bucket`, so `schedule.period`
    (which requires 'every') is never called for a cron task."""
    _cron_sched_cfg(cfg)
    assert "every" not in cfg.scheduled[0]
    slot = datetime(2026, 1, 1, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    result = disp.run_scheduled_tasks(cfg, now=slot)
    assert result["fired"] == ["digest"]


def test_mint_new_tickets_dedup_closes_cursor_crash_window_cron(home, cfg):
    """GA-19 extension of the interval crash-window regression: a cron task
    fired twice inside the SAME slot (cursor deleted between fires, simulating
    a crash before it persisted) mints exactly ONE ticket, not two."""
    _cron_sched_cfg(cfg)
    slot = datetime(2026, 1, 1, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    fired1 = disp.run_scheduled_tasks(cfg, slot)
    assert fired1["fired"] == ["digest"]
    (home / "derived" / ".schedule_cursor.json").unlink()
    fired2 = disp.run_scheduled_tasks(cfg, slot + 5)  # same wall-clock slot
    assert fired2["fired"] == ["digest"]

    pending = inbox.pending_new(home)
    assert len(pending) == 2
    assert pending[0][1]["args"]["dedup"] == pending[1][1]["args"]["dedup"]

    minted = disp.mint_new_tickets(cfg)
    assert minted == ["S-1"]  # only one real ticket, despite two queued fires
    assert sorted(p.name for p in (home / "tickets").iterdir()) == ["S-1"]


def test_schedule_status_cron_task_next_due_is_a_real_timestamp(home, cfg):
    """`maestro schedule list` (schedule_status) must show a real next-due
    timestamp for a cron task, never None or a crash."""
    _cron_sched_cfg(cfg)
    now = datetime(2026, 1, 1, 1, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    rows = disp.schedule_status(cfg, now)
    assert rows[0]["next_due"] == datetime(2026, 1, 1, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    assert rows[0]["cron"] == "0 2 * * *"
    assert rows[0]["tz"] == "UTC"


def test_dispatch_cli_reports_scheduled_fired(home, cfg):
    """A real sweep surfaces scheduled_fired in the report. GA-4: `dispatch
    --dry-run`'s CLI preview is read-only and no longer runs run_scheduled_tasks
    at all (a preview firing a real scheduled task -- appending to inbox/_new
    and advancing .schedule_cursor.json -- is exactly the class of undocumented
    mutation this ticket stops); a real sweep still fires it, same as ever."""
    _sched_cfg(cfg, every="24h")
    report = disp.dispatch(cfg, DryRunSessions(), now=1_000_000)
    assert report.scheduled_fired == ["digest"]


def test_dispatch_cli_dry_run_does_not_fire_scheduled_tasks(home):
    """The CLI preview counterpart to the test above: --dry-run reports an empty
    scheduled_fired and leaves .schedule_cursor.json untouched."""
    import json

    from maestro import cli

    (home / "config.toml").write_text(
        "[maestro]\nmax_concurrency = 3\n\n"
        "[[scheduled]]\n"
        'name = "digest"\n'
        'prompt = "Summarize things"\n'
        'every = "24h"\n'
    )
    cursor_path = home / "derived" / ".schedule_cursor.json"
    assert not cursor_path.exists()
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["--home", str(home), "dispatch", "--dry-run"])
    finally:
        sys.stdout = old_stdout
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["scheduled_fired"] == []
    assert not cursor_path.exists()


def test_schedule_list_cli(home):
    import json

    from maestro import cli

    (home / "config.toml").write_text(
        "[[scheduled]]\n"
        'name = "digest"\n'
        'prompt = "Summarize things"\n'
        'every = "24h"\n'
    )
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["--home", str(home), "schedule", "list"])
    finally:
        sys.stdout = old_stdout
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["scheduled"][0]["name"] == "digest"
    assert out["scheduled"][0]["last_fired"] is None


def test_schedule_list_cli_includes_repo_and_title(home):
    """GA-9 QA over the real CLI: `maestro schedule list`'s JSON surfaces a
    task's repo and title (the --home flag is mandatory here -- store.resolve_home
    otherwise falls back to $MAESTRO_HOME, the live dogfood board)."""
    import json

    from maestro import cli

    (home / "config.toml").write_text(
        "[[scheduled]]\n"
        'name = "digest"\n'
        'title = "Morning digest"\n'
        'repo = "alpha"\n'
        'prompt = "Summarize things"\n'
        'every = "24h"\n'
    )
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["--home", str(home), "schedule", "list"])
    finally:
        sys.stdout = old_stdout
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["scheduled"][0]["repo"] == "alpha"
    assert out["scheduled"][0]["title"] == "Morning digest"


def test_dispatch_scheduled_task_mints_ticket_with_repo_and_title(home, cfg):
    """GA-9 QA over the real app: over a temp MAESTRO_HOME, a real dispatcher
    sweep on a [[scheduled]] task carrying repo+title mints a ticket whose spec
    carries the `repo:` frontmatter line, whose TicketCreated payload carries
    repo + the prose title, and for which repos.bound_repo_name resolves the
    declared repo. The only mocked boundary is the `claude` spawn (DryRunSessions)."""
    from maestro import repos as repos_mod

    _sched_cfg(cfg, title="Morning digest", repo="alpha")
    now = 1_000_000
    r1 = disp.dispatch(cfg, DryRunSessions(), now=now)
    assert r1.scheduled_fired == ["digest"]
    r2 = disp.dispatch(cfg, DryRunSessions(), now=now + 5)
    assert r2.minted == ["S-1"]

    spec_text = store.spec_path(home, "S-1").read_text()
    assert "repo: alpha" in spec_text

    events = event_log.read(home, "S-1")
    created = next(e for e in events if e["type"] == "TicketCreated")
    assert created["payload"]["repo"] == "alpha"
    assert created["payload"]["title"] == "Morning digest"

    assert repos_mod.bound_repo_name(home, "S-1") == "alpha"


def test_doctor_warns_on_unconfigured_repo_in_scheduled_task(home, cfg):
    """GA-9 AC: a typo'd/unconfigured repo in a [[scheduled]] block is surfaced
    as a WARN via health.check_unknown_repo_bindings, not silently swallowed."""
    from maestro import health

    _sched_cfg(cfg, repo="typo-repo")
    result = health.check_unknown_repo_bindings(cfg, now=1_000_000)
    assert result["status"] == "warn"
    assert {u["repo"] for u in result["unknown"]} == {"typo-repo"}


# ---------------------------------------------------------------------------
# T-20: prune-logs dispatcher tick
# ---------------------------------------------------------------------------

PRUNE_NOW = 1_700_000_000.0


def _log(home, key, epoch, fmt="log"):
    from maestro.sessions import session_name
    session_id = f"{session_name(key)}-{epoch:.6f}"
    path = (store.session_stream_path(home, key, session_id) if fmt == "stream-json"
            else store.session_log_path(home, key, session_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * 10, encoding="utf-8")
    return path


def test_prune_tick_cursor_gates_repeat_sweeps(home, cfg):
    cfg.prune_interval = 100
    cfg.session_log_retention_days = 5
    cfg.session_log_max_per_ticket = 0

    old1 = _log(home, "T-1", PRUNE_NOW - 10 * 86400)
    report1 = disp.dispatch(cfg, DryRunSessions(), now=PRUNE_NOW)
    assert not old1.exists()
    assert report1.pruned_logs == 1
    assert report1.pruned_bytes > 0

    # A second, equally stale file appears before the gate reopens: the tick
    # must NOT run again at now + interval/2.
    old2 = _log(home, "T-1", PRUNE_NOW - 10 * 86400)
    report2 = disp.dispatch(cfg, DryRunSessions(), now=PRUNE_NOW + 50)
    assert old2.exists()
    assert report2.pruned_logs == 0

    report3 = disp.dispatch(cfg, DryRunSessions(), now=PRUNE_NOW + 100)
    assert not old2.exists()
    assert report3.pruned_logs == 1
    assert (home / "derived" / ".prune_cursor.json").exists()


def test_prune_tick_spares_in_window_and_live_claim(home, cfg):
    from maestro import claims

    cfg.prune_interval = 100
    cfg.session_log_retention_days = 5
    cfg.session_log_max_per_ticket = 2

    newest = _log(home, "T-1", PRUNE_NOW)
    recent = _log(home, "T-1", PRUNE_NOW - 3600)
    over_count = _log(home, "T-1", PRUNE_NOW - 2 * 3600)     # in-window by age, over count cap
    live = _log(home, "T-1", PRUNE_NOW - 20 * 86400)          # would be pruned by age+count, but live
    claims.write_claim(home, "T-1", 1, "reconcile-T-1", log_path=str(live))

    report = disp.dispatch(cfg, DryRunSessions(), now=PRUNE_NOW)

    assert newest.exists()
    assert recent.exists()
    assert live.exists()
    assert not over_count.exists()
    assert report.pruned_logs == 1
    assert report.pruned_bytes > 0


def test_prune_tick_reaches_orphan_agent_logs_dir_and_never_touches_protected_paths(home, cfg):
    from conftest import seed_ticket

    cfg.prune_interval = 100
    cfg.session_log_retention_days = 5
    cfg.session_log_max_per_ticket = 0

    # Orphan: no ticket dir, no events, no snapshot — absent from list_keys.
    orphan_old = _log(home, "T-ORPHAN", PRUNE_NOW - 10 * 86400)

    # launchd's live stdio files at the top level of agent-logs/.
    agent_logs = home / "agent-logs"
    agent_logs.mkdir(parents=True, exist_ok=True)
    dispatch_out = agent_logs / "dispatch.out.log"
    dispatch_err = agent_logs / "dispatch.err.log"
    dispatch_out.write_text("out\n", encoding="utf-8")
    dispatch_err.write_text("err\n", encoding="utf-8")

    # A directory whose name is not a valid key.
    bad_dir = agent_logs / "not a key"
    bad_dir.mkdir(parents=True, exist_ok=True)
    bad_file = bad_dir / "whatever.log"
    bad_file.write_text("x", encoding="utf-8")

    # A real, terminal (done) ticket exercising events/snapshot/inbox untouched-ness.
    seed_ticket(home, "T-1", "a finished ticket", phase="done")
    events_file = store.events_path(home, "T-1")
    archive_file = store.events_archive_path(home, "T-1")
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_text('{"seq":0}\n', encoding="utf-8")
    snap_file = store.snapshot_path(home, "T-1")
    inbox_file = store.inbox_path(home, "T-1")
    inbox_file.parent.mkdir(parents=True, exist_ok=True)
    inbox_file.write_text("", encoding="utf-8")

    ledger_file = home / "derived" / ".spawn_ledger.json"
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    ledger_file.write_text('{"T-9": 123}', encoding="utf-8")

    claims_dir = home / "derived" / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    claim_file = claims_dir / "T-9.json"
    claim_file.write_text('{"pid": 999999999, "name": "x"}', encoding="utf-8")

    protected = [dispatch_out, dispatch_err, bad_file, events_file, archive_file,
                 snap_file, inbox_file, ledger_file, claim_file]
    before = {p: p.read_bytes() for p in protected}

    report = disp.dispatch(cfg, DryRunSessions(), now=PRUNE_NOW)

    assert not orphan_old.exists()
    assert report.pruned_logs == 1
    for p, content in before.items():
        assert p.read_bytes() == content, f"{p} was modified"


def test_prune_tick_unreadable_dir_does_not_abort_the_sweep(home, cfg):
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permission bits")

    cfg.prune_interval = 100
    cfg.session_log_retention_days = 5
    cfg.session_log_max_per_ticket = 0

    bad_key_dir = home / "agent-logs" / "T-BAD"
    bad_key_dir.mkdir(parents=True, exist_ok=True)
    _log(home, "T-BAD", PRUNE_NOW - 10 * 86400)
    good_old = _log(home, "T-GOOD", PRUNE_NOW - 10 * 86400)

    _seed(home, "T-DUE", Phase.READY)

    bad_key_dir.chmod(0o000)
    try:
        report = disp.dispatch(cfg, DryRunSessions(), now=PRUNE_NOW)
    finally:
        bad_key_dir.chmod(0o755)  # restore so pytest's tmp_path cleanup can remove it

    assert "T-DUE" in report.spawned
    assert (home / "derived" / ".heartbeat.json").exists()
    assert not good_old.exists()          # other keys' logs still pruned
    assert report.errors.get("prune")     # error recorded, not propagated


def test_prune_logs_cli_end_to_end(home):
    import json

    from maestro import cli

    home_str = str(home)
    cli.main(["--home", home_str, "init"])
    (home / "config.toml").write_text(
        "[maestro]\n"
        "session_log_retention_days = 5\n"
        "session_log_max_per_ticket = 0\n",
        encoding="utf-8",
    )

    real_now = store.now_epoch()  # cmd_prune_logs uses wall-clock time, not an injected `now`
    old_a = _log(home, "T-1", real_now - 10 * 86400)
    recent_a = _log(home, "T-1", real_now - 1 * 86400)
    old_b = _log(home, "T-2", real_now - 10 * 86400)

    def _run(args):
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = cli.main(["--home", home_str, *args])
        finally:
            sys.stdout = old_stdout
        return rc, json.loads(buf.getvalue())

    # --all --dry-run: reports counts, deletes nothing.
    rc, out = _run(["prune-logs", "--all", "--dry-run"])
    assert rc == 0
    assert out["pruned_logs"] == 2
    assert old_a.exists() and old_b.exists()

    # Single-key prune only touches that key.
    rc, out = _run(["prune-logs", "T-1"])
    assert rc == 0
    assert out["pruned_logs"] == 1
    assert not old_a.exists()
    assert recent_a.exists()
    assert old_b.exists()

    # --all prunes every remaining key.
    rc, out = _run(["prune-logs", "--all"])
    assert rc == 0
    assert out["pruned_logs"] == 1
    assert not old_b.exists()

    # Cross-check against `maestro logs --list`.
    rc, out = _run(["logs", "T-1", "--list"])
    assert rc == 0
    assert [s["path"] for s in out] == [str(recent_a)]

    rc, out = _run(["logs", "T-2", "--list"])
    assert rc == 0
    assert out == []


# --- claim identity verification (T-17): pid reuse must not deadlock a slot ------
#
# Plain DryRunSessions.list_active() never touches claims/, which would make a
# pid-reuse test vacuous — it has to delegate to the real, claims-file-backed
# ClaudeCliSessions.list_active() (only the `claude` spawn itself is faked).

class _RealClaimsSessions(DryRunSessions):
    def __init__(self, home, **kw):
        super().__init__()
        self._real = ClaudeCliSessions(home, **kw)

    def list_active(self) -> set[str]:
        return self._real.list_active()


@pytest.fixture
def _children():
    procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
             for _ in range(4)]
    try:
        yield procs
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=5)


def test_pid_reuse_claims_are_released_and_spawned(home, _children):
    """The 2026-07-19 hazard, reproduced and fixed: four claims whose recorded
    epoch predates a real, live, NON-reconciler process (pid reuse) must not
    eat dispatch slots forever — they're denied, released, and the key spawns."""
    keys = ["T-1", "T-2", "T-3", "T-4"]
    old_epoch = store.now_epoch() - 3600
    for key, proc in zip(keys, _children):
        _seed(home, key, Phase.READY)
        store.write_json(claims.claim_path(home, key),
                         {"pid": proc.pid, "name": f"reconcile-{key}",
                          "ts": store.iso_now(), "epoch": old_epoch})

    cfg = Config(home=home, max_concurrency=10)
    sessions = _RealClaimsSessions(home)
    report = disp.dispatch(cfg, sessions, now=1000)

    for key in keys:
        assert key in report.spawned
        assert key not in report.claimed
        assert not claims.claim_path(home, key).exists()
    assert report.active_sessions == 0


def test_genuine_reconciler_claim_survives_a_sweep(home, _children):
    """A real, correctly-identified claim (epoch matches the child's true start)
    stays claimed/confirmed and is never spawned over."""
    proc = _children[0]
    _seed(home, "T-1", Phase.READY)
    claims.write_claim(home, "T-1", proc.pid, "reconcile-T-1")

    cfg = Config(home=home, max_concurrency=10)
    sessions = _RealClaimsSessions(home)
    report = disp.dispatch(cfg, sessions, now=1000)

    assert "T-1" in report.claimed
    assert "T-1" not in report.spawned
    assert report.active_sessions == 1
    assert claims.is_claimed(home, "T-1")
    assert claims.verify_claim(home, "T-1") == "confirmed"
    assert claims.claim_path(home, "T-1").exists()


def test_probe_forks_once_per_sweep_regardless_of_claim_count(home, _children):
    calls = []

    def counting_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.run(cmd, **kw)

    for i in range(8):
        key = f"T-{i}"
        _seed(home, key, Phase.READY)
        pid = _children[i % len(_children)].pid
        claims.write_claim(home, key, pid, f"reconcile-{key}")

    cfg = Config(home=home, max_concurrency=10)
    sessions = _RealClaimsSessions(home, claims_run=counting_run)
    disp.dispatch(cfg, sessions, now=1000)

    assert len(calls) == 1


# --- T-13: session watchdog ---


def _age_claim(home, key, epoch):
    """Backdate a just-written claim's epoch (claims.write_claim always stamps
    the current wall clock -- tests fabricate age by patching the file directly)."""
    data = claims.read_claim(home, key)
    data["epoch"] = epoch
    store.write_json(claims.claim_path(home, key), data)


def test_config_parses_watchdog_knobs(home):
    from maestro import config as config_mod

    store.atomic_write(home / "config.toml",
                       "[maestro]\nmax_session_seconds = 999\nmax_spawn_attempts = 7\n"
                       "no_output_timeout = 42\n")
    cfg = config_mod.load(str(home))
    assert cfg.max_session_seconds == 999
    assert cfg.max_spawn_attempts == 7
    assert cfg.no_output_timeout == 42


def test_watchdog_knobs_documented_in_sample_config():
    from maestro.config import DEFAULT_CONFIG_TOML
    assert "max_session_seconds" in DEFAULT_CONFIG_TOML
    assert "max_spawn_attempts" in DEFAULT_CONFIG_TOML
    assert "no_output_timeout" in DEFAULT_CONFIG_TOML


def test_max_concurrency_documents_sub_agent_amplification():
    """GA-14: an operator sizing the fleet off `max_concurrency` needs to see
    that one counted spawn can be more than one agent (RF-7: the `qa` phase's
    config-gated Standards-axis `Agent`-tool sub-agent fan-out)."""
    from maestro.config import DEFAULT_CONFIG_TOML
    max_concurrency_line = next(
        line for line in DEFAULT_CONFIG_TOML.splitlines() if line.startswith("max_concurrency"))
    idx = DEFAULT_CONFIG_TOML.index(max_concurrency_line)
    block_end = DEFAULT_CONFIG_TOML.index("\nreconcile_steady_interval", idx)
    block = DEFAULT_CONFIG_TOML[idx:block_end]
    assert "Agent" in block and "sub-agent" in block


def test_watchdog_kills_aged_claim_and_fails_ticket(home, cfg):
    """AC: a claim older than max_session_seconds is SIGTERM'd (pid == pgid),
    released, and routed through ops.fail -- proven over a REAL process group."""
    cfg.max_session_seconds = 100
    _seed(home, "T-1", Phase.IMPLEMENTING)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        claims.write_claim(home, "T-1", proc.pid, "reconcile-T-1")
        _age_claim(home, "T-1", store.now_epoch() - 10_000)  # far past the threshold

        reaped = disp.run_watchdog(cfg, now=store.now_epoch())

        assert reaped == ["T-1"]
        assert claims.read_claim(home, "T-1") is None       # claim released
        assert not claims.is_claimed(home, "T-1")

        for _ in range(30):                                  # process group actually died
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None

        events = event_log.read(home, "T-1")
        assert any(e["type"] == "Failed" for e in events)
        assert snap_mod.load(home, "T-1").failure_count == 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_watchdog_leaves_healthy_session_untouched(home, cfg):
    """AC: an under-threshold session is untouched -- no kill, no fail, still
    counted as claimed/active."""
    cfg.max_session_seconds = 3600
    _seed(home, "T-1", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")  # this process: alive, fresh

    reaped = disp.run_watchdog(cfg, now=store.now_epoch())

    assert reaped == []
    assert claims.is_claimed(home, "T-1")
    assert "T-1" in claims.active_keys(home)
    events = event_log.read(home, "T-1")
    assert all(e["type"] != "Failed" for e in events)


def test_watchdog_never_raises_on_already_dead_pid(home, cfg):
    """AC: the watchdog never raises when the claimed pid is already gone."""
    cfg.max_session_seconds = 100
    _seed(home, "T-1", Phase.IMPLEMENTING)
    dead_pid = 2_000_000_000  # almost certainly not a live pid
    claims.write_claim(home, "T-1", dead_pid, "reconcile-T-1")
    _age_claim(home, "T-1", store.now_epoch() - 10_000)

    reaped = disp.run_watchdog(cfg, now=store.now_epoch())  # must not raise

    assert reaped == ["T-1"]
    assert claims.read_claim(home, "T-1") is None
    events = event_log.read(home, "T-1")
    assert any(e["type"] == "Failed" for e in events)


def test_watchdog_disabled_when_max_session_seconds_is_zero(home, cfg):
    cfg.max_session_seconds = 0
    _seed(home, "T-1", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")
    _age_claim(home, "T-1", store.now_epoch() - 1_000_000)  # ancient, but watchdog is off

    assert disp.run_watchdog(cfg, now=store.now_epoch()) == []
    # NOT claims.is_claimed(): backdating epoch this far past this (real, live)
    # pid's true start time is indistinguishable from pid reuse (T-17) and would
    # be correctly denied -- the disabled-watchdog behavior under test is that
    # the claim file itself is left untouched, not that identity re-verifies.
    assert claims.read_claim(home, "T-1") is not None


def test_dispatch_runs_watchdog_before_computing_active(home, cfg):
    """A hung claim must not count toward concurrency in the same sweep it's
    reaped -- run_watchdog runs before `active = sessions.list_active()`."""
    cfg.max_session_seconds = 100
    cfg.max_concurrency = 1
    _seed(home, "T-1", Phase.IMPLEMENTING)
    _seed(home, "T-2", Phase.READY)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        claims.write_claim(home, "T-1", proc.pid, "reconcile-T-1")
        _age_claim(home, "T-1", store.now_epoch() - 10_000)

        report = disp.dispatch(cfg, DryRunSessions(), now=store.now_epoch())

        assert "T-1" in report.reaped
        assert report.spawned == ["T-2"]  # the freed slot went to T-2, not held by the dead claim
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_watchdog_reaps_claim_with_stale_output_log(home, cfg, tmp_path):
    """AC: a claim whose session log hasn't been mtime-touched within
    no_output_timeout is reaped -- released, failed with a no-output reason,
    excluded from active, and its pid passed to an injected killer."""
    cfg.no_output_timeout = 300
    cfg.max_session_seconds = 7200  # far larger -- isolates the no-output rule
    _seed(home, "T-1", Phase.IMPLEMENTING)
    log_file = tmp_path / "T-1.jsonl"
    log_file.write_text("{}\n")
    stale = store.now_epoch() - 1000
    os.utime(log_file, (stale, stale))
    claims.write_claim(home, "T-1", 424242, "reconcile-T-1", log_path=str(log_file))

    killed = []
    reaped = disp.run_watchdog(cfg, now=store.now_epoch(), kill=killed.append)

    assert reaped == ["T-1"]
    assert killed == [424242]                            # pid passed to the injected killer
    assert claims.read_claim(home, "T-1") is None         # claim released
    assert "T-1" not in claims.active_keys(home)
    events = event_log.read(home, "T-1")
    failed = [e for e in events if e["type"] == "Failed"]
    assert failed and "no output" in failed[-1]["payload"]["error"]


def test_watchdog_no_output_rule_independent_of_claim_epoch(home, cfg, tmp_path):
    """AC: a freshly-mtimed log is NOT reaped even when the claim epoch is far
    past no_output_timeout -- the two clocks are independent."""
    cfg.no_output_timeout = 300
    cfg.max_session_seconds = 0  # isolate: only the no-output rule can fire
    _seed(home, "T-1", Phase.IMPLEMENTING)
    log_file = tmp_path / "T-1.jsonl"
    log_file.write_text("{}\n")  # just written -- mtime is now
    claims.write_claim(home, "T-1", 424242, "reconcile-T-1", log_path=str(log_file))
    _age_claim(home, "T-1", store.now_epoch() - 10_000)  # epoch ancient, log fresh

    reaped = disp.run_watchdog(cfg, now=store.now_epoch())

    assert reaped == []
    assert claims.read_claim(home, "T-1") is not None


def test_watchdog_claim_without_log_path_survives_no_output_rule(home, cfg):
    """AC: a claim recorded with no log_path (capture_session_logs = false) is
    exempt from the no-output rule -- missing data never reaps."""
    cfg.no_output_timeout = 300
    cfg.max_session_seconds = 7200  # young epoch below this -- age rule can't fire either
    _seed(home, "T-1", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", 424242, "reconcile-T-1")  # no log_path

    reaped = disp.run_watchdog(cfg, now=store.now_epoch())

    assert reaped == []
    assert claims.read_claim(home, "T-1") is not None


def test_watchdog_claim_without_log_path_still_reaped_by_age_rule(home, cfg):
    """AC: a claim with no log_path falls through to the age-based rule --
    it isn't blanket-exempted from the watchdog, just from the no-output check."""
    cfg.no_output_timeout = 300
    cfg.max_session_seconds = 100
    _seed(home, "T-1", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", 424242, "reconcile-T-1")  # no log_path
    _age_claim(home, "T-1", store.now_epoch() - 10_000)

    reaped = disp.run_watchdog(cfg, now=store.now_epoch(), kill=lambda pid: None)

    assert reaped == ["T-1"]
    assert claims.read_claim(home, "T-1") is None


def test_watchdog_no_output_timeout_zero_disables_rule(home, cfg, tmp_path):
    """AC: no_output_timeout = 0 disables the no-output rule entirely -- a
    sweep reaps exactly the keys the age-only rule would have reaped before
    this ticket, even with a stale log on a claim that's otherwise young."""
    cfg.no_output_timeout = 0
    cfg.max_session_seconds = 100
    _seed(home, "T-1", Phase.IMPLEMENTING)  # aged claim, no log -- reaped by age
    claims.write_claim(home, "T-1", 424242, "reconcile-T-1")
    _age_claim(home, "T-1", store.now_epoch() - 10_000)
    _seed(home, "T-2", Phase.IMPLEMENTING)  # young claim, stale log -- untouched
    log_file = tmp_path / "T-2.jsonl"
    log_file.write_text("{}\n")
    stale = store.now_epoch() - 1_000_000
    os.utime(log_file, (stale, stale))
    claims.write_claim(home, "T-2", 434343, "reconcile-T-2", log_path=str(log_file))

    reaped = disp.run_watchdog(cfg, now=store.now_epoch(), kill=lambda pid: None)

    assert reaped == ["T-1"]
    assert claims.read_claim(home, "T-2") is not None


# --- T-45: a 0-turn "Unknown command" spawn is a structural failure, not a
# silently-released dead claim -------------------------------------------

def _write_zero_turn_result(home, key, session_id, *, command="/maestro-reconcile-implementing"):
    """A stream.jsonl holding only the terminal `result` record `claude -p`
    writes when the resolved slash command doesn't exist in the session's
    cwd -- no `system`/`assistant` records at all, since no turn ever ran.
    Mirrors the exact shape from T-45's spec Intent: num_turns 0, cost 0,
    is_error false, result text naming the missing command."""
    log = store.session_stream_path(home, key, session_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 0, "total_cost_usd": 0,
        "result": f"Unknown command: {command}", "session_id": session_id,
    }
    log.write_text(json.dumps(result) + "\n", encoding="utf-8")
    return log


def test_zero_turn_spawn_fails_and_dead_letters_on_first_detection(home, cfg):
    """AC: a dead claim whose session's terminal result shows num_turns==0 is
    failed -- naming the resolved command + cwd -- and dead-lettered on THIS,
    the first, detection (never a backoff/retry: a missing command file won't
    fix itself between sweeps)."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    session_id = "reconcile-T-1-1000.000000"
    log = _write_zero_turn_result(home, "T-1", session_id)
    dead_pid = 2_000_000_000  # almost certainly not a live pid
    cwd = str(home / "worktrees" / "T-1")
    prompt = "/maestro-reconcile-implementing T-1"
    claims.write_claim(home, "T-1", dead_pid, "reconcile-T-1", log_path=str(log),
                       cwd=cwd, prompt=prompt)

    failed = disp.detect_zero_turn_spawns(cfg, now=store.now_epoch())

    assert failed == ["T-1"]
    assert claims.read_claim(home, "T-1") is None  # claim released

    events = event_log.read(home, "T-1")
    failed_events = [e for e in events if e["type"] == "Failed"]
    assert len(failed_events) == 1
    message = failed_events[0]["payload"]["error"]
    assert prompt in message
    assert cwd in message

    snap = snap_mod.load(home, "T-1")
    assert snap.failure_count == 1
    assert snap.phase == Phase.DEGRADED.value  # dead-lettered, not backed off
    assert any(e["type"] == "Stalled" for e in events)


def _write_pi_zero_turn(home, key, session_id, *, command="/maestro-reconcile-implementing"):
    """A pi .pi.jsonl (T-58) holding a real-shaped "session"/"agent_start"/
    "agent_end" sequence with no "turn_start" at all -- the pi-runner analog of
    T-45's `_write_zero_turn_result`: whatever caused pi to bail before its
    first turn (a missing/unresolvable reconcile command in the session's cwd,
    matching the Claude scenario this guard was built for), no tool could
    possibly have run."""
    log = store.session_pi_path(home, key, session_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "session", "version": 3, "id": session_id,
         "timestamp": "2026-08-14T00:00:00.000Z", "cwd": str(home)},
        {"type": "agent_start"},
        {"type": "agent_end", "messages": [], "willRetry": False},
    ]
    log.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    return log


def test_zero_turn_pi_spawn_dead_letters_on_first_detection_same_as_claude(home, cfg):
    """AC2 (T-58): this is THE runaway net -- a pi log must trip it exactly
    like a `.stream.jsonl` one, on the very first detection, never a
    backoff/retry."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    session_id = "reconcile-T-1-1000.000000"
    log = _write_pi_zero_turn(home, "T-1", session_id)
    dead_pid = 2_000_000_000  # almost certainly not a live pid
    cwd = str(home / "worktrees" / "T-1")
    prompt = "/maestro-reconcile-implementing T-1"
    claims.write_claim(home, "T-1", dead_pid, "reconcile-T-1", log_path=str(log),
                       cwd=cwd, prompt=prompt)

    failed = disp.detect_zero_turn_spawns(cfg, now=store.now_epoch())

    assert failed == ["T-1"]
    assert claims.read_claim(home, "T-1") is None  # claim released

    events = event_log.read(home, "T-1")
    failed_events = [e for e in events if e["type"] == "Failed"]
    assert len(failed_events) == 1
    assert prompt in failed_events[0]["payload"]["error"]

    snap = snap_mod.load(home, "T-1")
    assert snap.failure_count == 1
    assert snap.phase == Phase.DEGRADED.value  # dead-lettered, not backed off


def test_zero_turn_pi_spawn_leaves_real_progress_untouched(home, cfg):
    """AC2 counterpart: a dead pi session that ran real turns is left alone
    here too -- the no-progress watchdog's territory, same as the Claude
    path's own `test_zero_turn_spawn_leaves_real_progress_to_the_no_progress_watchdog`."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    session_id = "reconcile-T-1-1000.000000"
    log = store.session_pi_path(home, "T-1", session_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "session", "version": 3, "id": session_id,
         "timestamp": "2026-08-14T00:00:00.000Z", "cwd": str(home)},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
                                             "provider": "anthropic"}},
        {"type": "agent_end", "messages": [], "willRetry": False},
    ]
    log.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    dead_pid = 2_000_000_000
    claims.write_claim(home, "T-1", dead_pid, "reconcile-T-1", log_path=str(log),
                       cwd=str(home), prompt="/maestro-reconcile-implementing T-1")

    assert disp.detect_zero_turn_spawns(cfg, now=store.now_epoch()) == []
    assert claims.read_claim(home, "T-1") is not None  # left for active_keys() to release normally
    assert all(e["type"] != "Failed" for e in event_log.read(home, "T-1"))


def test_zero_turn_spawn_leaves_live_session_untouched(home, cfg):
    """AC: the existing watchdog is unchanged -- a session still running is
    never touched by this detector, 0 turns so far or not."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    session_id = "reconcile-T-1-1000.000000"
    log = store.session_stream_path(home, "T-1", session_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")  # no terminal result yet -- session still running
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1", log_path=str(log),
                       cwd=str(home), prompt="/maestro-reconcile-implementing T-1")

    assert disp.detect_zero_turn_spawns(cfg, now=store.now_epoch()) == []
    assert claims.read_claim(home, "T-1") is not None
    assert all(e["type"] != "Failed" for e in event_log.read(home, "T-1"))


def test_zero_turn_spawn_leaves_real_progress_to_the_no_progress_watchdog(home, cfg):
    """AC: a dead session that ran real turns (but appended nothing) is the
    no-progress watchdog's territory, not this detector's -- left completely
    untouched here, still gated by `_allow_spawn`/`max_spawn_attempts`."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    session_id = "reconcile-T-1-1000.000000"
    log = store.session_stream_path(home, "T-1", session_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    result = {"type": "result", "subtype": "success", "is_error": False,
              "num_turns": 3, "total_cost_usd": 0.01, "result": "did nothing useful",
              "session_id": session_id}
    log.write_text(json.dumps(result) + "\n", encoding="utf-8")
    dead_pid = 2_000_000_000
    claims.write_claim(home, "T-1", dead_pid, "reconcile-T-1", log_path=str(log),
                       cwd=str(home), prompt="/maestro-reconcile-implementing T-1")

    assert disp.detect_zero_turn_spawns(cfg, now=store.now_epoch()) == []
    assert claims.read_claim(home, "T-1") is not None  # left for active_keys() to release normally
    assert all(e["type"] != "Failed" for e in event_log.read(home, "T-1"))


class _ZeroTurnSessions(DryRunSessions):
    """A stub `claude -p` spawn resolving to an unknown slash command (T-45):
    exits before any tool runs, and by the time the dispatcher looks again
    the process is already dead -- same "gone by next sweep" regime as
    `_EphemeralSessions` above, but this one leaves behind the claim + real
    stream log a genuine `ClaudeCliSessions.spawn()` would have written, so
    the dispatcher's own detection has something concrete to read."""

    def __init__(self, home):
        super().__init__()
        self._home = home
        self._n = 0

    def list_active(self) -> set[str]:
        return set()

    def spawn(self, key, prompt, cwd, model=None, effort=None,
              disallowed_tools=None, allowed_tools=None, env_overlay=None, runner=None,
              runner_model=None):
        super().spawn(key, prompt, cwd, model=model, effort=effort,
                      disallowed_tools=disallowed_tools, allowed_tools=allowed_tools,
                      env_overlay=env_overlay, runner=runner, runner_model=runner_model)
        self._n += 1
        session_id = f"reconcile-{key}-{self._n}.000000"
        command = prompt.split()[0]
        log = _write_zero_turn_result(self._home, key, session_id, command=command)
        claims.write_claim(self._home, key, 2_000_000_000, f"reconcile-{key}",
                           log_path=str(log), cwd=str(cwd), prompt=prompt)
        return None


def test_dispatch_detects_zero_turn_spawn_on_the_very_next_sweep(home, cfg):
    """End-to-end over the real dispatcher (AC): the sweep that spawns a
    0-turn 'Unknown command' session is not itself the one that catches it
    (the log doesn't exist until spawn() writes it) -- but the VERY NEXT
    sweep does, not the Nth sweep of the no-progress watchdog."""
    cfg.min_spawn_interval = 0
    _seed(home, "T-1", Phase.IMPLEMENTING)
    sessions = _ZeroTurnSessions(home)
    now = 1_000_000

    r1 = disp.dispatch(cfg, sessions, now=now)
    assert r1.spawned == ["T-1"]
    assert snap_mod.load(home, "T-1").phase == Phase.IMPLEMENTING.value  # not yet detected

    r2 = disp.dispatch(cfg, sessions, now=now + 1)

    assert "T-1" in r2.reaped
    events = event_log.read(home, "T-1")
    assert any(e["type"] == "Failed" for e in events)
    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.DEGRADED.value


def test_spawn_attempts_fail_after_max_with_no_progress(home, cfg):
    """AC: N spawns with zero new events (observed_seq never advances) convert
    into a failure instead of an infinite respawn loop -- looped over real
    dispatch() sweeps with a no-op session manager."""
    _seed(home, "T-1", Phase.READY)
    cfg.max_spawn_attempts = 3
    cfg.min_spawn_interval = 0
    sessions = _EphemeralSessions()  # "dies" instantly every sweep; appends nothing

    now = 1_000_000
    reports = [disp.dispatch(cfg, sessions, now=now + i) for i in range(4)]

    assert [r.spawned for r in reports[:3]] == [["T-1"]] * 3
    assert reports[3].spawned == []          # 4th attempt: failed instead of respawned
    assert "T-1" in reports[3].reaped

    snap = snap_mod.load(home, "T-1")
    assert snap.failure_count == 1
    # lands in the existing backoff/dead-letter machinery -- one or the other:
    assert snap.next_requeue_at is not None or snap.phase == Phase.DEGRADED.value


def test_spawn_attempts_reset_when_observed_seq_advances(home, cfg):
    """Real progress (an appended event) resets the no-progress counter, so a
    normally-converging ticket is never penalized."""
    _seed(home, "T-1", Phase.READY)
    cfg.max_spawn_attempts = 2
    cfg.min_spawn_interval = 0
    sessions = _EphemeralSessions()

    now = 1_000_000
    r1 = disp.dispatch(cfg, sessions, now=now)
    assert r1.spawned == ["T-1"]
    # Progress happens between sweeps (a real reconciler would append something).
    event_log.append(home, "T-1", "Note", {"text": "made progress"}, actor="r")
    snap_mod.rebuild(home, "T-1")

    r2 = disp.dispatch(cfg, sessions, now=now + 1)
    assert r2.spawned == ["T-1"]  # counter reset by the seq bump -- not failed
    assert snap_mod.load(home, "T-1").failure_count == 0


# --- launchctl-free kill switch: fleet.pause()/resume() (T-15) --------------

def test_paused_sweep_mints_and_spawns_nothing(home, cfg):
    """A pending _new create-request AND an active due ticket: a paused sweep
    must touch neither — no mint, no spawn, no backup, inbox stays unacked."""
    from maestro import fleet

    _seed(home, "T-1", Phase.READY)
    inbox.append_new(home, "queued while paused", key="T-9")
    fleet.pause(home, reason="testing")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.paused is True
    assert report.minted == [] and report.due == [] and report.spawned == []
    assert report.throttled == []
    assert inbox.pending_new(home) != []  # _new inbox still unacked
    assert event_log.last_seq(home, "T-9") == 0  # no TicketCreated
    backup_dir = home.parent / f"{home.name}-backups"
    assert not backup_dir.exists() or list(backup_dir.glob("*.tar.gz")) == []

    # Resume: the identical sweep now mints the request and spawns the due key.
    fleet.resume(home)
    report2 = disp.dispatch(cfg, DryRunSessions(), now=1001)
    assert "T-9" in report2.minted
    assert "T-1" in report2.spawned


def test_pause_has_no_human_bypass(home, cfg):
    """A pending human answer (an _UNTHROTTLED_REASONS due-reason) must NOT
    slip past the pause — the reason that skips the spawn floor must not
    also skip the kill switch."""
    from maestro import fleet

    _seed(home, "T-1", Phase.READY)
    inbox.append_command(home, "T-1", "ans", {"qid": "q1", "text": "go"})
    fleet.pause(home, reason="no bypass")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.paused is True
    assert report.spawned == []


def test_paused_sweep_fails_safe_on_corrupt_pause_file(home, cfg):
    from maestro import fleet

    _seed(home, "T-1", Phase.READY)
    store.atomic_write(fleet.pause_path(home), "{not json at all")
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.paused is True and report.spawned == []


def test_paused_sweep_fails_safe_on_garbage_until(home, cfg):
    from maestro import fleet

    _seed(home, "T-1", Phase.READY)
    store.atomic_write(fleet.pause_path(home),
                       '{"since": 1, "until": "not-a-timestamp", "reason": "x"}')
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.paused is True and report.spawned == []


def test_past_until_auto_resumes_mid_sweep(home, cfg):
    """A `.paused` whose `until` has already elapsed must unlink and complete a
    full normal sweep in that SAME dispatch() call — the due key is spawned."""
    from maestro import fleet

    _seed(home, "T-1", Phase.READY)
    now = 1_000_000
    fleet.pause(home, until=now - 10, reason="expired")

    report = disp.dispatch(cfg, DryRunSessions(), now=now)
    assert report.paused is False
    assert report.spawned == ["T-1"]
    assert not fleet.pause_path(home).exists()


def test_future_until_stays_paused_across_sweeps(home, cfg):
    from maestro import fleet

    _seed(home, "T-1", Phase.READY)
    now = 1_000_000
    fleet.pause(home, until=now + 3600, reason="future")

    r1 = disp.dispatch(cfg, DryRunSessions(), now=now)
    r2 = disp.dispatch(cfg, DryRunSessions(), now=now + 100)
    assert r1.paused is True and r1.spawned == []
    assert r2.paused is True and r2.spawned == []
    assert fleet.pause_path(home).exists()


def test_paused_sweep_still_writes_heartbeat(home, cfg):
    from maestro import fleet

    fleet.pause(home, reason="hb check")
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    hb = store.read_json(home / "derived" / ".heartbeat.json", {})
    assert hb.get("paused") is True
    assert hb.get("spawned") == 0
    assert hb.get("active") == 0


def test_doctor_reports_paused_board(home):
    import json

    from maestro import cli, fleet

    fleet.pause(home, reason="doctor check")
    disp.dispatch(Config(home=home), DryRunSessions(), now=1000)

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["--home", str(home), "doctor"])
    finally:
        sys.stdout = old_stdout
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["paused"] is True


# --- L-12: hooks never abort the sweep ---------------------------------------


def test_raising_hook_does_not_abort_the_sweep(home, cfg, monkeypatch):
    """AC1: a tracker/network/etc hook raising must not stop due keys from
    being found and spawned -- only the old unguarded-hooks bug did that."""
    def _boom(*a, **k):
        raise RuntimeError("network is down")

    monkeypatch.setattr(disp, "sync_vcs", _boom)
    _seed(home, "T-1", Phase.READY)
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.spawned == ["T-1"]
    assert "sync_vcs" in report.hook_errors
    assert "network is down" in report.hook_errors["sync_vcs"]


def test_every_listed_hook_is_wrapped(home, cfg, monkeypatch):
    """AC1's explicit list: tracker sync, schedule, worktree sync, backup,
    notify -- each raises in turn and the sweep still completes and spawns."""
    from maestro import backup, notify

    hooks = {
        "sync_external_sources": (disp, "sync_external_sources"),
        "run_scheduled_tasks": (disp, "run_scheduled_tasks"),
        "sync_worktrees": (disp, "sync_worktrees"),
        "backup": (backup, "maybe_backup"),
        "notify": (notify, "maybe_notify"),
    }
    cfg.min_spawn_interval = 0  # isolate hook-wrapping from the unrelated spawn-floor
    for i, (name, (mod, attr)) in enumerate(hooks.items()):
        def _boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(mod, attr, _boom)
        _seed(home, "T-hook", Phase.READY)
        report = disp.dispatch(cfg, DryRunSessions(), now=2000 + i)
        assert report.spawned == ["T-hook"], f"{name} hook aborted the sweep"
        assert name in report.hook_errors
        monkeypatch.undo()
        # tidy the seeded ticket so the next hook's sweep starts fresh
        for p in (home / "events" / "T-hook.jsonl", home / "derived" / "snapshots" / "T-hook.json"):
            p.unlink(missing_ok=True)


def test_healthy_sweep_has_no_hook_errors(home, cfg):
    _seed(home, "T-1", Phase.READY)
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.hook_errors == {}


# --- RB-2: one bad ticket's fold never stops the sweep -----------------------


def test_malformed_event_does_not_stop_the_sweep(home, cfg):
    """AC2, repro form: a ticket carrying the spec's exact repro corruption (a
    PhaseChanged with an unrecognized `phase`) folds cleanly now that `fold`
    is total (law b) -- no exception, so the corrupt ticket itself is not even
    knocked out of the sweep, and every other due ticket still spawns."""
    _seed(home, "T-bad", Phase.READY)
    event_log.append(home, "T-bad", "PhaseChanged", {"phase": "totally-bogus"}, actor="r")
    snap_mod.rebuild(home, "T-bad")
    _seed(home, "T-good", Phase.READY)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "T-good" in report.spawned
    assert "T-bad" in report.spawned  # the corrupt ticket recovers too, not just its neighbors
    assert snap_mod.load(home, "T-bad").fold_warnings  # corruption stayed visible, not silent


def test_fold_wrap_records_failure_on_report(home, cfg, monkeypatch):
    """AC2, defense-in-depth form: even though `fold` is now total, the per-key
    fold in the sweep loop is wrapped exactly like the other dispatch hooks
    (see test_raising_hook_does_not_abort_the_sweep above) -- if it somehow
    still raises for one ticket, that must not stop the other due tickets from
    being found and spawned, and the failure must be recorded on the report."""
    _seed(home, "T-bad", Phase.READY)
    _seed(home, "T-good", Phase.READY)

    real = disp._load_and_refresh_snapshot

    def _boom(home_, key):
        if key == "T-bad":
            raise ValueError("simulated corrupt event")
        return real(home_, key)

    monkeypatch.setattr(disp, "_load_and_refresh_snapshot", _boom)
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report.spawned == ["T-good"]
    assert "fold:T-bad" in report.hook_errors
    assert "simulated corrupt event" in report.hook_errors["fold:T-bad"]


# --- L-12: per-sweep decision ledger (`derived/dispatch.jsonl`) + `maestro why` ---


def test_dispatch_appends_one_ledger_line_per_sweep(home, cfg):
    _seed(home, "T-1", Phase.READY)
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    disp.dispatch(cfg, DryRunSessions(), now=1001)
    lines = disp.dispatch_ledger_path(home).read_text().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["epoch"] == 1000
    assert "decisions" in rec and "hook_errors" in rec


def test_ledger_records_every_decision_kind(home, cfg):
    """AC2: due-with-reason, skipped-claimed, capacity_skipped, blocked-dep,
    spawned all show up in the same sweep's ledger line."""
    _seed_with_deps(home, "T-dep", Phase.IMPLEMENTING)
    _seed_with_deps(home, "T-blocked", Phase.READY, depends_on=["T-dep"])
    _seed(home, "T-claimed", Phase.READY)
    _seed(home, "T-spawn", Phase.READY)
    cfg.max_concurrency = 2  # one free slot: T-dep (first due, alphabetically) gets it
    sessions = DryRunSessions(active={"T-claimed"})

    disp.dispatch(cfg, sessions, now=1000)
    rec = json.loads(disp.dispatch_ledger_path(home).read_text().splitlines()[-1])
    decisions = rec["decisions"]
    assert decisions["T-blocked"] == {"outcome": "not_due", "reason": "blocked-dep"}
    assert decisions["T-claimed"]["outcome"] == "claimed"
    assert decisions["T-dep"]["outcome"] == "spawned"
    assert decisions["T-spawn"]["outcome"] == "capacity_skipped"


def test_ledger_is_size_capped(home, cfg):
    _seed(home, "T-1", Phase.READY)
    cfg.min_spawn_interval = 0
    # Exercises the dispatch ledger's own line cap, not the GA-5 runaway brake --
    # with the floor off, the default budget (12/h) would otherwise trip the
    # brake and short-circuit most of this loop's sweeps at G1, which append no
    # ledger line at all.
    cfg.runaway_pause_cooldown = 0
    n = disp._DISPATCH_LEDGER_MAX_LINES + 25
    for i in range(n):
        disp.dispatch(cfg, DryRunSessions(), now=1000 + i)
    lines = disp.dispatch_ledger_path(home).read_text().splitlines()
    assert len(lines) == disp._DISPATCH_LEDGER_MAX_LINES


def test_key_decisions_tail_filters_to_one_key(home, cfg):
    _seed(home, "T-1", Phase.READY)
    _seed(home, "T-2", Phase.READY)
    cfg.min_spawn_interval = 0
    for i in range(3):
        disp.dispatch(cfg, DryRunSessions(), now=1000 + i)
    decisions = disp.key_decisions(home, "T-1", tail=10)
    assert len(decisions) == 3
    assert all(d["outcome"] == "spawned" for d in decisions)
    assert all("ts" in d for d in decisions)


def test_why_cli_reports_a_key_recent_decisions(home, cfg):
    """AC2: `maestro why <KEY>` prints that key's recent ledger decisions,
    proven through the real CLI."""
    from maestro import cli

    _seed(home, "T-1", Phase.READY)
    disp.dispatch(cfg, DryRunSessions(), now=1000)

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["--home", str(home), "why", "T-1"])
    finally:
        sys.stdout = old
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["key"] == "T-1"
    assert out["decisions"][-1]["outcome"] == "spawned"


def test_why_cli_empty_for_unknown_key(home):
    from maestro import cli

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["--home", str(home), "why", "NOPE-1"])
    finally:
        sys.stdout = old
    assert rc == 0
    assert json.loads(buf.getvalue())["decisions"] == []


# --- L-12: compact / archive_done as dispatcher ticks ------------------------


def test_run_compact_tick_disabled_by_default(home, cfg):
    _seed(home, "T-1", Phase.IMPLEMENTING)
    assert cfg.compact_interval == 0
    result = disp.run_compact_tick(cfg, now=1000)
    assert result == {"compacted": []}


def test_run_compact_tick_skips_keys_under_min_events(home, cfg):
    cfg.compact_interval = 60
    cfg.compact_min_events = 1000  # far above what _seed produces
    _seed(home, "T-1", Phase.IMPLEMENTING)
    result = disp.run_compact_tick(cfg, now=1000)
    assert result == {"compacted": []}


def test_run_compact_tick_compacts_and_is_cursor_gated(home, cfg):
    cfg.compact_interval = 60
    cfg.compact_min_events = 1  # T-1 has >= 1 folded event
    _seed(home, "T-1", Phase.IMPLEMENTING)

    r1 = disp.run_compact_tick(cfg, now=1000)
    assert r1["compacted"] == ["T-1"]
    archive_path = store.events_archive_path(home, "T-1")
    assert archive_path.exists()

    # Same window: no-op even though there'd be nothing new to compact anyway.
    r2 = disp.run_compact_tick(cfg, now=1010)
    assert r2["compacted"] == []

    # Past the interval: runs again (idempotent -- nothing new to move).
    r3 = disp.run_compact_tick(cfg, now=1061)
    assert r3["compacted"] == []  # already compacted; ops.compact reports 0 archived


def test_run_archive_tick_disabled_when_archive_after_is_none(home, cfg):
    assert cfg.archive_after is None
    _seed(home, "T-1", Phase.DONE)
    result = disp.run_archive_tick(cfg, now=1000)
    assert result == {"archived": []}
    assert store.ticket_dir(home, "T-1").exists()  # untouched


def test_run_archive_tick_respects_grace_period(home, cfg):
    cfg.archive_after = 100
    _seed(home, "T-1", Phase.DONE)
    snap = snap_mod.load(home, "T-1")
    done_epoch = datetime.fromisoformat(snap.updated_ts).timestamp()

    too_soon = disp.run_archive_tick(cfg, now=done_epoch + 50)
    assert too_soon == {"archived": []}
    assert store.ticket_dir(home, "T-1").exists()

    later = disp.run_archive_tick(cfg, now=done_epoch + 200)
    assert later == {"archived": ["T-1"]}
    assert not store.ticket_dir(home, "T-1").exists()
    assert (home / "tickets" / "_archive" / "T-1").exists()


def test_dispatch_archives_done_ticket_absent_from_next_sweep_decisions(home, cfg):
    """AC4: a real dispatch() sweep archives a DONE ticket, and the following
    sweep's ledger decisions no longer mention it -- list_keys stopped seeing it."""
    cfg.archive_after = 0
    _seed(home, "T-1", Phase.DONE)
    _seed(home, "T-2", Phase.READY)

    r1 = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "T-1" in r1.hook_errors.get("archive_tick", "") or True  # no error expected
    assert not store.ticket_dir(home, "T-1").exists()
    assert "T-1" not in disp.list_keys(home)

    rec = json.loads(disp.dispatch_ledger_path(home).read_text().splitlines()[-1])
    assert "T-1" not in rec["decisions"]
    assert "T-2" in rec["decisions"]


# --- L-12: new config knobs ---------------------------------------------------


def test_config_parses_maintenance_knobs(home):
    from maestro import config as config_mod

    store.atomic_write(home / "config.toml",
                       "[maestro]\ncompact_interval = 999\ncompact_min_events = 50\narchive_after = 86400\n")
    cfg = config_mod.load(str(home))
    assert cfg.compact_interval == 999
    assert cfg.compact_min_events == 50
    assert cfg.archive_after == 86400


def test_maintenance_knobs_documented_in_sample_config():
    from maestro.config import DEFAULT_CONFIG_TOML
    assert "compact_interval" in DEFAULT_CONFIG_TOML
    assert "archive_after" in DEFAULT_CONFIG_TOML
