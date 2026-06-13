from maestro import dispatcher as disp
from maestro import event_log, inbox, ops, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _seed(home, key, phase=Phase.READY):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated", {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def test_active_phase_is_due(home):
    _seed(home, "T-1", Phase.READY)
    snap = snap_mod.load(home, "T-1")
    res = disp.is_due(snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=1000)
    assert res.due and res.reason == "active"


def test_sleeping_phase_not_due_until_signal(home):
    _seed(home, "T-1", Phase.AWAITING_HUMAN)
    snap = snap_mod.load(home, "T-1")
    assert not disp.is_due(snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=1000).due
    # inbox arrival wakes it
    assert disp.is_due(snap, inbox_pending=True, current_spec_hash=snap.spec_hash, now=1000).due


def test_spec_edit_wakes_sleeping_ticket(home):
    _seed(home, "T-1", Phase.AWAITING_HUMAN)
    snap = snap_mod.load(home, "T-1")
    res = disp.is_due(snap, inbox_pending=False, current_spec_hash="DIFFERENT", now=1000)
    assert res.due and res.reason == "spec-changed"


def test_requeue_timer_wakes_awaiting_ci(home, cfg):
    _seed(home, "T-1", Phase.AWAITING_CI)
    ops.requeue(cfg, "T-1", 100)
    snap = snap_mod.load(home, "T-1")
    base = snap.next_requeue_at
    assert not disp.is_due(snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=base - 1).due
    assert disp.is_due(snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=base + 1).due


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


def test_worker_cwd_prefers_existing_worktree(home):
    cfg = Config(home=home, repo_path=str(home / "repo"))
    wt = home / "worktrees" / "T-1"
    wt.mkdir(parents=True)
    assert disp._worker_cwd(cfg, "T-1") == wt


def test_worker_cwd_falls_back_to_repo_before_worktree_exists(home, tmp_path):
    # The first triage step runs before a worktree exists; it must land in the
    # repo so the /maestro-reconcile command + skill resolve (home has neither).
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config(home=home, repo_path=str(repo))
    assert disp._worker_cwd(cfg, "T-1") == repo


def test_worker_cwd_last_resort_is_home(home):
    cfg = Config(home=home, repo_path=None)
    assert disp._worker_cwd(cfg, "T-1") == home
