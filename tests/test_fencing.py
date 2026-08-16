"""RB-7: the CAS on `event_log.append` (`expected_last_seq`) is wired onto the
state-machine gate, `ops.set_phase` -- and nowhere else. Every caller that
already holds the folded snapshot it decided the target phase from
(`ops.ask`/`ask_round`, `ops.route_conflict`/`route_stale`,
`dispatcher._observe_ci`/`_observe_reviews`, and `maestro set-phase --expect`
through the CLI) threads `expect=<that observed_seq>` through, so a decision
made from a fold that went stale before the append reached the lock is
rejected (`StaleAppendError`) instead of silently committing. A lost race is
benign by construction: it is left to propagate uncaught, never spends
`failure_count`, and the dispatcher's next sweep re-derives the ticket from
the now-current log.
"""
from maestro import dispatcher as disp, event_log, ops, snapshot as snap_mod, store
from maestro.cli import main
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _seed(cfg, key, phase=Phase.READY, tier=0):
    store.atomic_write(store.spec_path(cfg.home, key),
                       f"# {key}\napproval_tier: {tier}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(cfg.home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(cfg.home, key)}, actor="d")
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(cfg.home, key)


# --- AC5: a stale caller is rejected through the real CLI, no failure_count spend,
# and a real dispatch() sweep re-derives the ticket next pass. -------------------

def test_stale_expect_rejected_via_real_cli_no_failure_count_spend(cfg, capsys):
    key = "T-STALE"
    _seed(cfg, key, Phase.READY)
    stale = snap_mod.load(cfg.home, key).observed_seq

    # A concurrent writer (a human, another dispatcher tick, a second reconciler)
    # appends after the fold the CLI caller below is about to act on.
    event_log.append(cfg.home, key, "Note", {"text": "concurrent writer"}, actor="other")

    rc = main(["--home", str(cfg.home), "set-phase", key, "implementing", "--expect", str(stale)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "expected tail" in err

    after = snap_mod.load(cfg.home, key)
    assert after.phase == Phase.READY.value  # rejected -- no PhaseChanged landed
    assert after.failure_count == 0  # a lost race is not a failure
    assert not [e for e in event_log.read(cfg.home, key) if e["type"] == "Failed"]
    assert not [e for e in event_log.read(cfg.home, key) if e["type"] == "PhaseChanged"
               and e["payload"].get("phase") == Phase.IMPLEMENTING.value]

    # The dispatcher's next sweep re-derives the ticket from the current log,
    # exactly as if the rejected CLI call had never been attempted.
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert key in report.spawned


def test_correct_expect_succeeds_via_real_cli(cfg):
    key = "T-FRESH"
    _seed(cfg, key, Phase.READY)
    fresh = snap_mod.load(cfg.home, key).observed_seq

    rc = main(["--home", str(cfg.home), "set-phase", key, "implementing", "--expect", str(fresh)])
    assert rc == 0
    assert snap_mod.load(cfg.home, key).phase == Phase.IMPLEMENTING.value


def test_set_phase_without_expect_is_unfenced_as_before(cfg):
    """No --expect (the CLI default) preserves today's ergonomics -- a human
    typing a bare `maestro set-phase` doesn't need to know observed_seq."""
    key = "T-PLAIN"
    _seed(cfg, key, Phase.READY)
    event_log.append(cfg.home, key, "Note", {"text": "unrelated"}, actor="other")

    rc = main(["--home", str(cfg.home), "set-phase", key, "implementing"])
    assert rc == 0
    assert snap_mod.load(cfg.home, key).phase == Phase.IMPLEMENTING.value


# --- ops.py internal callers thread expect=<their own held fold> ---------------

def test_ask_rejects_when_raced_between_its_own_fold_and_the_phase_transition(cfg, monkeypatch):
    """`ops.ask` appends QuestionAsked, reloads the fold that decision is made
    from, and passes that observed_seq as `expect`. Simulate a writer racing in
    right after that reload -- the AWAITING_HUMAN transition must lose the CAS."""
    key = "T-ASK"
    _seed(cfg, key, Phase.IMPLEMENTING)

    real_load = snap_mod.load

    def racy_load(home, k):
        snap = real_load(home, k)
        if k == key:
            event_log.append(home, k, "Note", {"race": True}, actor="other")
        return snap

    monkeypatch.setattr(ops.snap_mod, "load", racy_load)

    try:
        ops.ask(cfg, key, "are we done?")
        raised = False
    except event_log.StaleAppendError:
        raised = True
    assert raised

    snap = snap_mod.load(cfg.home, key)
    assert snap.phase == Phase.IMPLEMENTING.value  # never reached awaiting-human
    assert snap.failure_count == 0


def test_ask_succeeds_normally_without_a_race(cfg):
    key = "T-ASK-2"
    _seed(cfg, key, Phase.IMPLEMENTING)
    ops.ask(cfg, key, "are we done?")
    snap = snap_mod.load(cfg.home, key)
    assert snap.phase == Phase.AWAITING_HUMAN.value


def test_ask_round_rejects_when_raced(cfg, monkeypatch):
    key = "T-ASKROUND"
    _seed(cfg, key, Phase.IMPLEMENTING)

    real_load = snap_mod.load

    def racy_load(home, k):
        snap = real_load(home, k)
        if k == key:
            event_log.append(home, k, "Note", {"race": True}, actor="other")
        return snap

    monkeypatch.setattr(ops.snap_mod, "load", racy_load)

    try:
        ops.ask_round(cfg, key, [("q1?", None, None), ("q2?", None, None)])
        raised = False
    except event_log.StaleAppendError:
        raised = True
    assert raised
    assert snap_mod.load(cfg.home, key).phase == Phase.IMPLEMENTING.value


def test_route_conflict_rejects_when_raced_between_fold_and_transition(cfg, monkeypatch):
    key = "T-CONFLICT"
    _seed(cfg, key, Phase.AWAITING_CI)

    real_load = snap_mod.load

    def racy_load(home, k):
        snap = real_load(home, k)
        if k == key:
            event_log.append(home, k, "Note", {"race": True}, actor="other")
        return snap

    monkeypatch.setattr(ops.snap_mod, "load", racy_load)

    try:
        ops.route_conflict(cfg, key, 42, actor="dispatcher")
        raised = False
    except event_log.StaleAppendError:
        raised = True
    assert raised
    snap = snap_mod.load(cfg.home, key)
    assert snap.phase == Phase.AWAITING_CI.value
    assert snap.failure_count == 0


def test_route_conflict_succeeds_normally_without_a_race(cfg):
    key = "T-CONFLICT-2"
    _seed(cfg, key, Phase.AWAITING_CI)
    moved = ops.route_conflict(cfg, key, 42, actor="dispatcher")
    assert moved is True
    assert snap_mod.load(cfg.home, key).phase == Phase.IMPLEMENTING.value


def test_route_stale_rejects_when_raced(cfg, monkeypatch):
    key = "T-DRIFT"
    _seed(cfg, key, Phase.AWAITING_CI)

    real_load = snap_mod.load

    def racy_load(home, k):
        snap = real_load(home, k)
        if k == key:
            event_log.append(home, k, "Note", {"race": True}, actor="other")
        return snap

    monkeypatch.setattr(ops.snap_mod, "load", racy_load)

    try:
        ops.route_stale(cfg, key, actor="dispatcher")
        raised = False
    except event_log.StaleAppendError:
        raised = True
    assert raised
    assert snap_mod.load(cfg.home, key).phase == Phase.AWAITING_CI.value


# --- dispatcher.py: sync_vcs isolates a stale race to one key, never crashes
# the whole sweep, never spends failure_count, and a sibling key is unaffected. --

class _FakeVCS:
    def __init__(self, statuses):
        self.statuses = statuses

    def pr_for_branch(self, branch, repo=None, env=None):
        return None

    def pr_status(self, pr_number, repo=None, env=None):
        return self.statuses[pr_number]

    def review_feedback(self, pr_number, repo=None, env=None):
        return []


def _seed_pr(cfg, key, pr, phase=Phase.AWAITING_CI):
    store.atomic_write(store.spec_path(cfg.home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(cfg.home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(cfg.home, key)}, actor="d")
    event_log.append(cfg.home, key, "PrOpened",
                     {"number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": False},
                     actor="r")
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(cfg.home, key)


def test_sync_vcs_stale_race_on_one_key_is_isolated_from_its_sibling(cfg, monkeypatch):
    from maestro import providers

    fake = _FakeVCS({
        42: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha-a",
             "ci_state": "failing", "failing_checks": ["unit"]},
        43: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha-b",
             "ci_state": "failing", "failing_checks": ["unit"]},
    })
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": 0}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)

    _seed_pr(cfg, "T-A", 42)
    _seed_pr(cfg, "T-B", 43)

    real_rebuild = snap_mod.rebuild

    def racy_rebuild(home, key):
        snap = real_rebuild(home, key)
        if key == "T-A":
            # Simulate a concurrent writer landing between _observe_ci's fold
            # and the set_phase call it decides from that fold.
            event_log.append(home, key, "Note", {"race": True}, actor="other")
        return snap

    monkeypatch.setattr(disp.snap_mod, "rebuild", racy_rebuild)

    result = disp.sync_vcs(cfg, now=1000)
    assert result["checked"] == 2

    snap_a = snap_mod.load(cfg.home, "T-A")
    snap_b = snap_mod.load(cfg.home, "T-B")
    assert snap_a.phase == Phase.AWAITING_CI.value  # lost the race, unmoved
    assert snap_a.failure_count == 0  # no dead-letter spend for a benign race
    assert not [e for e in event_log.read(cfg.home, "T-A") if e["type"] == "Failed"]
    # the CiObserved itself (before the raced transition) still landed
    assert [e for e in event_log.read(cfg.home, "T-A") if e["type"] == "CiObserved"]

    assert snap_b.phase == Phase.IMPLEMENTING.value  # sibling processed normally
    assert "unit" in snap_b.failing_checks
