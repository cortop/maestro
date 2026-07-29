import io
import os
import subprocess
import sys
import time

import pytest

from maestro import claims, dispatcher as disp
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


def _ask(home, key, qid="q1", text="ok?"):
    """Give an awaiting-human ticket a real open question (the production state)."""
    event_log.append(home, key, "QuestionAsked", {"qid": qid, "text": text}, actor="r")
    snap_mod.rebuild(home, key)


def test_sleeping_phase_not_due_until_signal(home):
    _seed(home, "T-1", Phase.AWAITING_HUMAN)
    _ask(home, "T-1")  # an open question is what makes awaiting-human legitimately sleep
    snap = snap_mod.load(home, "T-1")
    assert not disp.is_due(snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=1000).due
    # inbox arrival wakes it
    assert disp.is_due(snap, inbox_pending=True, current_spec_hash=snap.spec_hash, now=1000).due


def test_spec_edit_wakes_sleeping_ticket(home):
    _seed(home, "T-1", Phase.AWAITING_HUMAN)
    _ask(home, "T-1")
    snap = snap_mod.load(home, "T-1")
    res = disp.is_due(snap, inbox_pending=False, current_spec_hash="DIFFERENT", now=1000)
    assert res.due and res.reason == "spec-changed"


def test_stranded_awaiting_human_is_due(home, cfg):
    """awaiting-human with no open question, no answered question, and no timer is
    stranded — the dispatcher must wake it so it can never sleep forever."""
    _seed(home, "T-1", Phase.AWAITING_HUMAN)  # bare phase set, never asked
    snap = snap_mod.load(home, "T-1")
    assert not snap.open_questions and not snap.answered_questions
    res = disp.is_due(snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=1000)
    assert res.due and res.reason == "stranded"
    # A real sweep spawns a reconciler for the stranded ticket (recovery happens there).
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "T-1" in report.spawned


def test_requeue_timer_wakes_awaiting_ci(home, cfg):
    _seed(home, "T-1", Phase.AWAITING_CI)
    ops.requeue(cfg, "T-1", 100)
    snap = snap_mod.load(home, "T-1")
    base = snap.next_requeue_at
    assert not disp.is_due(snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=base - 1).due
    assert disp.is_due(snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=base + 1).due


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


@pytest.mark.parametrize("phase", [Phase.IN_REVIEW, Phase.IMPLEMENTING,
                                   Phase.TRIAGING, Phase.READY, Phase.DEGRADED])
def test_requeue_timer_holds_any_non_terminal_phase(home, cfg, phase):
    """A reconciler that asks to sleep is obeyed from EVERY phase, not just the
    two in SLEEPING_PHASES. The in-review case is the 2026-07-19 runaway: the
    handler ends in `maestro requeue $KEY 900` and the dispatcher ignored it."""
    _seed(home, "T-1", phase)
    ops.requeue(cfg, "T-1", 900)
    snap = snap_mod.load(home, "T-1")
    base = snap.next_requeue_at

    held = disp.is_due(snap, inbox_pending=False,
                       current_spec_hash=snap.spec_hash, now=base - 1)
    assert not held.due and held.reason == "backoff"

    woke = disp.is_due(snap, inbox_pending=False,
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
    """Real `maestro dispatch --dry-run` twice in a row surfaces the throttle."""
    import json

    from maestro import cli

    (home / "config.toml").write_text(
        "[maestro]\nmax_concurrency = 3\nmin_spawn_interval = 300\n")
    _seed(home, "T-1", Phase.READY)

    def _sweep():
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            assert cli.main(["--home", str(home), "dispatch", "--dry-run"]) == 0
        finally:
            sys.stdout = old
        return json.loads(buf.getvalue())

    assert _sweep()["would_spawn"] == ["T-1"]
    second = _sweep()
    assert second["would_spawn"] == [] and second["throttled"] == ["T-1"]


def test_spawn_ledger_records_rolling_history_and_trims_window(home, cfg):
    """dispatch() writes {key: {"last": float, "recent": [...]}}, and rate
    computation (health.spawn_rate) drops entries older than the window."""
    from maestro import health

    _seed(home, "T-1", Phase.IMPLEMENTING)  # active phase, no timer -> due every sweep
    cfg.min_spawn_interval = 0
    cfg.max_spawn_attempts = 0  # this test's ticket never progresses observed_seq by
    # design (it's exercising the ledger, not real reconcile steps); the no-progress
    # watchdog (T-13) would otherwise fail it after 5 spawns and starve `recent`.
    sessions = _EphemeralSessions()
    t0 = 1_000_000
    for i in range(10):
        disp.dispatch(cfg, sessions, now=t0 + i)  # 10 spawns, 1s apart

    ledger = store.read_json(disp._spawn_ledger_path(home), {})
    entry = ledger["T-1"]
    assert isinstance(entry, dict) and "last" in entry and "recent" in entry
    assert len(entry["recent"]) == 10
    assert health.spawn_rate(home, t0 + 9)["total"] == 10

    # Jump past the window and spawn once more: only the fresh entry counts.
    later = t0 + health.WINDOW_SECONDS + 100
    disp.dispatch(cfg, sessions, now=later)
    assert health.spawn_rate(home, later)["total"] == 1


def test_spawn_ledger_recent_hard_capped(home, cfg):
    """`recent` cannot grow without bound even with the spawn floor disabled."""
    _seed(home, "T-1", Phase.IMPLEMENTING)
    cfg.min_spawn_interval = 0
    cfg.max_spawn_attempts = 0  # no-progress watchdog would otherwise fail this
    # never-progressing ticket long before `recent` reaches the cap.
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


# --- RT-1: parse_spec_overrides ---

def test_parse_spec_overrides_empty():
    assert disp.parse_spec_overrides("approval_tier: 1\npriority: 3\n") == {}


def test_parse_spec_overrides_model_only():
    spec = "approval_tier: 1\nmodel: opus\ndependsOn: []\n"
    assert disp.parse_spec_overrides(spec) == {"model": "opus"}


def test_parse_spec_overrides_all_three():
    spec = "approval_tier: 1\nkind: research\nmodel: opus\neffort: high\ndependsOn: []\n"
    result = disp.parse_spec_overrides(spec)
    assert result == {"kind": "research", "model": "opus", "effort": "high"}


def test_parse_spec_overrides_stops_at_section_header():
    spec = "approval_tier: 1\n## Intent\nmodel: opus\n"
    assert disp.parse_spec_overrides(spec) == {}


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
    spawned_map = {k: (m, e) for k, _p, _c, m, e in sessions.spawned}
    assert spawned_map["T-1"] == ("opus", "high")


def test_dispatch_spawns_with_config_defaults_when_no_spec_overrides(home, cfg):
    """AC2: ticket with no overrides uses reconcile_model from config, no effort."""
    _seed_with_overrides(home, "T-1")
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    spawned_map = {k: (m, e) for k, _p, _c, m, e in sessions.spawned}
    assert spawned_map["T-1"] == (cfg.reconcile_model, None)


def test_dispatch_research_ticket_uses_research_defaults(home):
    """AC1 variant: kind=research uses research_model/research_effort from config."""
    from maestro.config import Config
    cfg = Config(home=home, max_concurrency=3, research_model="opus",
                 research_effort="high")
    _seed_with_overrides(home, "T-1", kind="research")
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    spawned_map = {k: (m, e) for k, _p, _c, m, e in sessions.spawned}
    assert spawned_map["T-1"] == ("opus", "high")


# --- RT-1: _seed_spec with new fields ---

def test_seed_spec_includes_kind_model_effort(home, cfg):
    inbox.append_new(home, "Research task", key="R-1", args={
        "approval_tier": 1, "priority": 2,
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
    assert "dependsOn: []" in spec_text


def test_mint_tolerates_explicit_null_fields(home, cfg):
    """A create-request carrying explicit JSON ``null`` for intent/args must not
    crash the whole sweep (regression: null intent made ``_seed_spec`` join a None)."""
    inbox.append_new(home, "Nullable ticket", prefix="M",
                     args={"approval_tier": 1, "priority": 3,
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
        "approval_tier": 0, "kind": "implementation", "priority": 3, "prefix": "S",
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
        "kind": "implementation", "approval_tier": 0, "priority": 3,
        "prefix": "S", "enabled": True, "last_fired": now, "next_due": now + 3600,
    }]


def test_schedule_status_never_fired_has_no_last_fired(home, cfg):
    _sched_cfg(cfg)
    rows = disp.schedule_status(cfg, now=1_000_000)
    assert rows[0]["last_fired"] is None
    assert rows[0]["next_due"] == 3600  # one period from the epoch


def test_dispatch_cli_reports_scheduled_fired(home):
    """Real 'maestro dispatch --dry-run' CLI surfaces scheduled_fired in the report."""
    import json

    from maestro import cli

    (home / "config.toml").write_text(
        "[maestro]\nmax_concurrency = 3\n\n"
        "[[scheduled]]\n"
        'name = "digest"\n'
        'prompt = "Summarize things"\n'
        'every = "24h"\n'
    )
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["--home", str(home), "dispatch", "--dry-run"])
    finally:
        sys.stdout = old_stdout
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["scheduled_fired"] == ["digest"]


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
                       "[maestro]\nmax_session_seconds = 999\nmax_spawn_attempts = 7\n")
    cfg = config_mod.load(str(home))
    assert cfg.max_session_seconds == 999
    assert cfg.max_spawn_attempts == 7


def test_watchdog_knobs_documented_in_sample_config():
    from maestro.config import DEFAULT_CONFIG_TOML
    assert "max_session_seconds" in DEFAULT_CONFIG_TOML
    assert "max_spawn_attempts" in DEFAULT_CONFIG_TOML


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
    assert claims.is_claimed(home, "T-1")


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
