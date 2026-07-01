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
