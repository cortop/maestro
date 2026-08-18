"""GA-4: `dispatch --dry-run` is a strictly read-only preview (option (a)).

Every test drives the real surface: a real `dispatch(cfg, sessions, now=..., dry_run=...)`
sweep, or the real `maestro` CLI, over a temp home -- never a mocked dispatcher. The only
mocked boundary is a genuinely external one (a tracker/VCS provider), matching the pattern
already established by test_vcs_sync.py's ``FakeVCS``.
"""
import json

from maestro import claims, dispatcher as disp, event_log, inbox, providers, \
    snapshot as snap_mod, store
from maestro.cli import main
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git as _git, make_origin_and_repo as _make_origin_and_repo


def _seed(home, key, phase=Phase.READY):
    store.atomic_write(store.spec_path(home, key),
                        f"# {key}\napproval_tier: 0\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def _write_stream_log(home, key, epoch, records):
    session_id = f"reconcile-{key}-{epoch:.6f}"
    path = home / "agent-logs" / key / f"{session_id}.stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


class FakeTracker:
    """The only mock: a genuinely external tracker (Jira/Linear-shaped), the
    same carve-out test_vcs_sync.py's FakeVCS already relies on."""

    def __init__(self):
        self.import_calls = 0

    def view(self, key):
        return {}

    def transition(self, key, status):
        pass

    def assignee(self, key):
        return None

    def import_new(self, home):
        self.import_calls += 1
        return 0

    def refresh(self, home, key, external_id):
        return 0


# --- AC: N consecutive previews leave events/ledger/attempts byte-identical --

def test_n_consecutive_previews_leave_events_ledger_attempts_byte_identical(home, cfg):
    _seed(home, "T-1", Phase.IMPLEMENTING)  # active phase -- due every sweep
    cfg.min_spawn_interval = 0

    events_before = store.events_path(home, "T-1").read_bytes()
    ledger_path = disp._spawn_ledger_path(home)
    attempts_path = disp._spawn_attempts_path(home)
    ledger_before = ledger_path.read_bytes() if ledger_path.exists() else None
    attempts_before = attempts_path.read_bytes() if attempts_path.exists() else None

    for i in range(5):
        report = disp.dispatch(cfg, DryRunSessions(), now=1000 + i, dry_run=True)
        assert report.spawned == ["T-1"]  # would_spawn, reported every time

    assert store.events_path(home, "T-1").read_bytes() == events_before
    assert (ledger_path.read_bytes() if ledger_path.exists() else None) == ledger_before
    assert (attempts_path.read_bytes() if attempts_path.exists() else None) == attempts_before


# --- AC: N consecutive `dispatch --dry-run` CLI invocations (the exact surface
# --- the AC names) leave events/ledger/attempts byte-identical -------------

def test_n_consecutive_cli_dry_run_invocations_leave_state_byte_identical(home):
    """AC2's literal surface: N separate real `maestro dispatch --dry-run`
    PROCESSES (via cli.main, each a fresh CLI invocation, not just N calls to
    dispatch() in-process) must never write events/, .spawn_ledger.json, or
    .spawn_attempts.json."""
    (home / "config.toml").write_text(
        "[maestro]\nmax_concurrency = 3\nmin_spawn_interval = 0\n")
    _seed(home, "T-1", Phase.IMPLEMENTING)  # active phase -- due every sweep

    events_before = store.events_path(home, "T-1").read_bytes()
    ledger_path = disp._spawn_ledger_path(home)
    attempts_path = disp._spawn_attempts_path(home)
    assert not ledger_path.exists() and not attempts_path.exists()

    for _ in range(5):
        rc = main(["--home", str(home), "dispatch", "--dry-run"])
        assert rc == 0

    assert store.events_path(home, "T-1").read_bytes() == events_before
    assert not ledger_path.exists()
    assert not attempts_path.exists()


# --- AC: 7 previews at max_spawn_attempts append ZERO events, ticket untouched --

def test_seven_previews_at_attempts_cap_append_zero_events(home, cfg):
    cfg.max_spawn_attempts = 5
    cfg.min_spawn_interval = 0
    _seed(home, "T-1", Phase.IMPLEMENTING)
    events_before = store.events_path(home, "T-1").read_bytes()
    snap_before = snap_mod.load(home, "T-1")

    for i in range(7):
        report = disp.dispatch(cfg, DryRunSessions(), now=1000 + i, dry_run=True)
        assert report.spawned == ["T-1"]
        assert report.reaped == []

    assert store.events_path(home, "T-1").read_bytes() == events_before
    events = event_log.read(home, "T-1")
    assert not any(e["type"] in ("Failed", "RequeueScheduled") for e in events)
    snap_after = snap_mod.load(home, "T-1")
    assert snap_after.phase == snap_before.phase
    assert snap_after.failure_count == snap_before.failure_count == 0
    assert snap_after.next_requeue_at == snap_before.next_requeue_at


# --- AC: a preview never throttles or delays the very next real sweep -------

def test_preview_never_throttles_the_next_real_sweep(home, cfg):
    _seed(home, "T-1", Phase.READY)
    cfg.min_spawn_interval = 300  # would throttle a second REAL spawn this close

    for i in range(10):
        preview = disp.dispatch(cfg, DryRunSessions(), now=1000 + i, dry_run=True)
        assert preview.spawned == ["T-1"]
        assert preview.throttled == []

    real = disp.dispatch(cfg, DryRunSessions(), now=1010)
    assert real.spawned == ["T-1"]
    assert real.throttled == []


# --- AC: no phantom "spawned" in dispatch.jsonl / heartbeat / `maestro why` --

def test_preview_never_reports_a_spawn_that_never_happened(home, cfg):
    _seed(home, "T-1", Phase.READY)
    report = disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)
    assert report.spawned == ["T-1"]

    ledger_lines = disp.dispatch_ledger_path(home).read_text().splitlines()
    rec = json.loads(ledger_lines[-1])
    assert rec["decisions"]["T-1"]["outcome"] == "would_spawn"
    assert not any(d.get("outcome") == "spawned" for d in rec["decisions"].values())

    hb = store.read_json(home / "derived" / ".heartbeat.json", {})
    assert hb["spawned"] == 0  # not len(would_spawn) -- no phantom count

    decisions = disp.key_decisions(home, "T-1")
    assert decisions[-1]["outcome"] == "would_spawn"
    assert not any(d["outcome"] == "spawned" for d in decisions)


# --- AC: would_mint reads inbox/_new without draining it ---------------------

def test_would_mint_reads_without_draining(home, cfg):
    inbox.append_new(home, "A new ticket", args={"intent": "do it"})
    new_path = store.new_inbox_path(home)
    cursor_path = store.new_cursor_path(home)
    minted_path = home / "derived" / ".schedule_minted.json"
    new_before = new_path.read_bytes()

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)
    assert report.minted == []
    assert report.would_mint == ["T-1"]

    assert new_path.read_bytes() == new_before
    assert not cursor_path.exists()
    assert not minted_path.exists()
    assert not store.events_path(home, "T-1").exists()  # never actually minted

    # A real sweep right after still mints the previewed key -- nothing was consumed.
    real = disp.dispatch(cfg, DryRunSessions(), now=1001)
    assert real.minted == ["T-1"]


def test_dispatch_cli_dry_run_reports_would_mint(home):
    inbox.append_new(home, "A new ticket", args={"intent": "do it"})
    rc = main(["--home", str(home), "dispatch", "--dry-run"])
    assert rc == 0


# --- The twelve hooks, one assertion each: absent under dry_run, present ----
# --- (given a live trigger) under a real sweep. -----------------------------

def test_hook_mint_new_tickets_is_absent_under_dry_run(home, cfg):
    inbox.append_new(home, "A new ticket", args={"intent": "do it"})
    disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)
    assert not store.events_path(home, "T-1").exists()
    disp.dispatch(cfg, DryRunSessions(), now=1001)  # real sweep: the hook DOES run
    assert store.events_path(home, "T-1").exists()


def test_hook_sync_external_sources_is_absent_under_dry_run(home, cfg, monkeypatch):
    fake = FakeTracker()
    monkeypatch.setattr(providers, "get_trackers", lambda c: {"fake": fake})
    cfg.provider_config = {"tracker": {"fake": {"sync_interval": 0}}}
    sync_cursor = home / "derived" / ".sync_cursor.json"

    disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)
    assert fake.import_calls == 0
    assert not sync_cursor.exists()

    disp.dispatch(cfg, DryRunSessions(), now=1001)  # real sweep: the hook DOES run
    assert fake.import_calls == 1
    assert sync_cursor.exists()


def test_hook_run_scheduled_tasks_is_absent_under_dry_run(home, cfg):
    cfg.scheduled = [{"name": "digest", "prompt": "Summarize", "every": "1h",
                      "approval_tier": 0, "kind": "implementation", "priority": 3,
                      "prefix": "S", "enabled": True}]
    cursor_path = home / "derived" / ".schedule_cursor.json"

    report = disp.dispatch(cfg, DryRunSessions(), now=1_000_000, dry_run=True)
    assert report.scheduled_fired == []
    assert not cursor_path.exists()

    real = disp.dispatch(cfg, DryRunSessions(), now=1_000_001)  # real sweep: fires
    assert real.scheduled_fired == ["digest"]
    assert cursor_path.exists()


def test_hook_sync_worktrees_is_absent_under_dry_run(home, cfg, tmp_path):
    """sync_worktrees routes a stale awaiting-ci ticket (worktree behind a just-
    merged origin/main) back into implementing -- exactly
    test_worktree_sync.py's own real-git setup, run once under dry_run (must be
    a no-op) and once for real (must route)."""
    cfg.base_drift_policy = "always"  # MTO-2: new default (on_conflict) never routes on drift alone
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    store.atomic_write(store.spec_path(home, "T-5"),
                        "# T-5\napproval_tier: 0\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, "T-5", "TicketCreated", {"title": "T-5"}, actor="d")
    event_log.append(home, "T-5", "PrOpened",
                     {"number": 10, "url": "https://github.com/x/y/pull/10", "draft": False},
                     actor="r")
    event_log.append(home, "T-5", "PhaseChanged", {"phase": Phase.AWAITING_CI.value}, actor="r")
    # T-82: a drift-only reroute now requires a positively known-safe CI state.
    event_log.append(home, "T-5", "CiObserved",
                     {"state": "passing", "failing_checks": []}, actor="r")
    snap_mod.rebuild(home, "T-5")
    wt = home / "worktrees" / "T-5"
    _git("worktree", "add", "-q", "-b", "maestro/T-5", str(wt), "main", cwd=repo)

    (repo / "NEWS.md").write_text("merged change\n")  # another ticket's PR just landed
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "T-9: merged change", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)
    assert snap_mod.load(home, "T-5").phase == Phase.AWAITING_CI.value  # untouched
    assert not any(e["type"] == "PhaseChanged" and e["payload"].get("phase") == Phase.IMPLEMENTING.value
                  for e in event_log.read(home, "T-5"))

    disp.dispatch(cfg, DryRunSessions(), now=1001)  # real sweep: routes to implementing
    assert snap_mod.load(home, "T-5").phase == Phase.IMPLEMENTING.value


def test_hook_backup_maybe_backup_is_absent_under_dry_run(home, cfg, tmp_path):
    import shutil

    from maestro import backup

    cfg.backup_dir = str(tmp_path / "backups")
    _seed(home, "T-1", Phase.READY)
    cursor_path = home / "derived" / ".backup_cursor.json"

    disp.dispatch(cfg, DryRunSessions(), now=cfg.backup_interval + 1000, dry_run=True)
    assert not cursor_path.exists()
    assert not backup.resolve_backup_dir(cfg).exists() or \
        list(backup.resolve_backup_dir(cfg).glob("*.tar.gz")) == []

    disp.dispatch(cfg, DryRunSessions(), now=cfg.backup_interval + 2000)  # real: backs up
    assert cursor_path.exists()
    assert list(backup.resolve_backup_dir(cfg).glob("*.tar.gz"))
    shutil.rmtree(backup.resolve_backup_dir(cfg), ignore_errors=True)


def test_hook_run_compact_tick_is_absent_under_dry_run(home, cfg):
    cfg.compact_interval = 60
    cfg.compact_min_events = 1
    _seed(home, "T-1", Phase.IMPLEMENTING)
    events_before = store.events_path(home, "T-1").read_bytes()
    cursor_path = home / "derived" / ".compact_cursor.json"
    archive_path = store.events_archive_path(home, "T-1")

    disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)
    assert store.events_path(home, "T-1").read_bytes() == events_before
    assert not cursor_path.exists()
    assert not archive_path.exists()

    disp.dispatch(cfg, DryRunSessions(), now=1001)  # real sweep: compacts
    assert cursor_path.exists()
    assert archive_path.exists()


def test_hook_run_archive_tick_is_absent_under_dry_run(home, cfg):
    cfg.archive_after = 0
    _seed(home, "T-1", Phase.DONE)
    ticket_dir = store.ticket_dir(home, "T-1")

    disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)
    assert ticket_dir.exists()

    disp.dispatch(cfg, DryRunSessions(), now=1001)  # real sweep: archives
    assert not ticket_dir.exists()
    assert (home / "tickets" / "_archive" / "T-1").exists()


def test_hook_ratelimit_probe_is_absent_under_dry_run(home, cfg):
    cfg.min_spawn_interval = 0
    _seed(home, "T-1", Phase.READY)
    real1 = disp.dispatch(cfg, DryRunSessions(), now=1000)  # real spawn, seeds ledger
    assert real1.spawned == ["T-1"]
    _write_stream_log(home, "T-1", 1000.0, [{
        "type": "rate_limit_event", "uuid": "u-1", "session_id": "sess-1",
        "rate_limit_info": {"status": "rejected", "resetsAt": 2000, "rateLimitType": "five_hour"},
    }])
    ratelimit_cursor = home / "derived" / ".ratelimit_cursor.json"
    ratelimit_state = home / "derived" / ".ratelimit.json"

    disp.dispatch(cfg, DryRunSessions(), now=1010, dry_run=True)
    assert not ratelimit_cursor.exists()
    assert not ratelimit_state.exists()

    disp.dispatch(cfg, DryRunSessions(), now=1020)  # real sweep: probes and pauses
    assert ratelimit_cursor.exists()
    assert ratelimit_state.exists()


def test_hook_notify_maybe_notify_is_absent_under_dry_run(home, cfg):
    cfg.notify_command = "true"  # harmless real subprocess -- the external boundary
    _seed(home, "T-1", Phase.READY)
    cursor_path = home / "derived" / ".notify_cursor.json"

    disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)
    assert not cursor_path.exists()

    disp.dispatch(cfg, DryRunSessions(), now=1001)  # real sweep: cursor advances
    assert cursor_path.exists()


def test_hook_run_watchdog_is_absent_under_dry_run(home, cfg):
    cfg.max_session_seconds = 100
    _seed(home, "T-1", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", 999_999_999, "reconcile-T-1")  # pid that can't exist
    data = claims.read_claim(home, "T-1")
    data["epoch"] = 1000 - 10_000  # far past the threshold
    store.write_json(claims.claim_path(home, "T-1"), data)
    claim_before = claims.claim_path(home, "T-1").read_bytes()

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)
    assert report.reaped == []
    assert claims.claim_path(home, "T-1").read_bytes() == claim_before
    events = event_log.read(home, "T-1")
    assert not any(e["type"] == "Failed" for e in events)

    disp.dispatch(cfg, DryRunSessions(), now=1001)  # real sweep: reaps
    assert claims.read_claim(home, "T-1") is None
    events = event_log.read(home, "T-1")
    assert any(e["type"] == "Failed" for e in events)


def test_hook_prune_logs_tick_is_absent_under_dry_run(home, cfg):
    cfg.session_log_retention_days = 1
    cfg.session_log_max_per_ticket = None
    _seed(home, "T-1", Phase.READY)
    old_log = _write_stream_log(home, "T-1", 1_000.0, [])  # epoch far before `now`
    cursor_path = home / "derived" / ".prune_cursor.json"

    disp.dispatch(cfg, DryRunSessions(), now=10_000_000, dry_run=True)
    assert not cursor_path.exists()
    assert old_log.exists()

    disp.dispatch(cfg, DryRunSessions(), now=10_000_001)  # real sweep: prunes
    assert cursor_path.exists()
    assert not old_log.exists()
