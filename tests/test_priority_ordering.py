"""MTO-7: `priority` governs dispatch order for real -- a preference over which
due ticket a capped sweep considers first, upstream of every existing brake
(spawn floor, repo-blocked, `max_spawns_per_sweep`, `max_concurrency` slots,
the runaway/rate-limit/spend-ceiling gates). It decides nothing about whether a
key is due, throttled, or spawnable -- only their relative order through those
unchanged filters. Every test drives a real `dispatch(cfg, sessions, ...)`
sweep over a temp home; the only substituted boundary is `DryRunSessions`."""
from maestro import dispatcher as disp, event_log, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _seed(home, key, *, priority=None, phase=Phase.READY):
    lines = [f"# {key}", "approval_tier: 0"]
    if priority is not None:
        lines.append(f"priority: {priority}")
    lines += ["", "## Acceptance criteria", "- [ ] ok"]
    store.atomic_write(store.spec_path(home, key), "\n".join(lines) + "\n")
    event_log.append(home, key, "TicketCreated",
                      {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(home, key)


# --- AC1/AC2: priority demonstrably changes spawn order under a cap ---------

def test_priority_1_spawns_ahead_of_lexically_earlier_priority_3(home):
    """A-1 sorts first lexically but is priority 3; B-1 is priority 1. Under a
    per-repo cap of max_spawns_per_sweep=1, the priority:1 ticket must win the
    single slot even though it is lexically later."""
    cfg = Config(home=home, max_concurrency=10, min_spawn_interval=0,
                 repos={"default": {"path": None, "slug": None, "base_branch": "main",
                                     "branch_prefix": "maestro/", "default": True,
                                     "max_spawns_per_sweep": 1}})
    _seed(home, "A-1", priority=3)
    _seed(home, "B-1", priority=1)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["B-1"]
    decision = disp.key_decisions(home, "A-1", tail=1)[0]
    assert decision["outcome"] == "repo_capped"


# --- AC3: total, deterministic order -- equal priority falls back to split_key

def test_equal_priority_falls_back_to_split_key(home):
    cfg = Config(home=home, max_concurrency=10, min_spawn_interval=0,
                 repos={"default": {"path": None, "slug": None, "base_branch": "main",
                                     "branch_prefix": "maestro/", "default": True,
                                     "max_spawns_per_sweep": 1}})
    _seed(home, "B-1", priority=2)
    _seed(home, "A-1", priority=2)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["A-1"]  # split_key tiebreak: A-1 before B-1


# --- AC4: missing/malformed priority falls back to the default without raising

def test_missing_priority_defaults_to_3(home):
    assert disp.spec_priority(home, "NOPE") == 3
    _seed(home, "T-1")  # no priority line at all
    assert disp.spec_priority(home, "T-1") == 3


def test_malformed_priority_defaults_to_3(home):
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\npriority: not-a-number\n")
    assert disp.spec_priority(home, "T-1") == 3


def test_malformed_priority_does_not_crash_a_real_sweep(home, cfg):
    store.atomic_write(store.spec_path(home, "T-1"),
                        "# T-1\napproval_tier: 0\npriority: not-a-number\n"
                        "\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, "T-1", "TicketCreated",
                      {"title": "T-1", "spec_hash": disp.spec_hash_on_disk(home, "T-1")}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.READY.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]


# --- AC5: priority never bypasses an existing brake -------------------------

def test_high_priority_throttled_ticket_does_not_starve_the_board(home):
    """T-2 is priority 1 (would sort first) but freshly spawn-floored; T-1 is
    priority 3 and eligible. The floor still applies -- T-1 spawns, T-2 stays
    throttled, and priority does not punch through min_spawn_interval."""
    cfg = Config(home=home, max_concurrency=10, min_spawn_interval=60)
    _seed(home, "T-1", priority=3)
    _seed(home, "T-2", priority=1)
    ledger_path = disp._spawn_ledger_path(home)
    store.write_json(ledger_path, {"T-2": {"last": 999}})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    assert "T-2" in report.throttled


def test_high_priority_does_not_bypass_max_concurrency_slots(home):
    """Two active sessions already fill max_concurrency=2; a priority:1 ticket
    still has to wait for a free slot like everyone else."""
    cfg = Config(home=home, max_concurrency=2, min_spawn_interval=0)
    _seed(home, "T-1", priority=3)
    _seed(home, "T-2", priority=3)
    _seed(home, "T-3", priority=1)
    sessions = DryRunSessions(active={"T-1", "T-2"})  # both slots already taken

    report = disp.dispatch(cfg, sessions, now=1000)

    assert report.spawned == []
    assert "T-3" in dict(report.due)
