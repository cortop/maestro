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
    res = disp.is_due(snap, inbox_pending=False,
                      current_spec_hash=snap.spec_hash, now=1000, blocked_dep=True)
    assert not res.due
    assert res.reason == "blocked-dep"


def test_ready_ticket_unblocked_when_dep_done(home, cfg):
    _seed_with_deps(home, "T-dep", Phase.DONE)
    _seed_with_deps(home, "T-1", Phase.READY, depends_on=["T-dep"])
    snap = snap_mod.load(home, "T-1")
    res = disp.is_due(snap, inbox_pending=False,
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
