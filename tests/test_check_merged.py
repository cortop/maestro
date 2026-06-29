"""ops.check_merged and the check-merged CLI command finalize a ticket from any phase."""
from maestro import event_log, ops, snapshot as snap_mod, store
from maestro.statemachine import Phase


def _create(cfg, key, *, phase=Phase.AWAITING_CI, pr_number=42):
    store.atomic_write(store.spec_path(cfg.home, key), f"# {key}\napproval_tier: 1\n")
    event_log.append(cfg.home, key, "TicketCreated", {"title": key}, actor="d")
    event_log.append(cfg.home, key, "PrOpened",
                     {"number": pr_number, "url": f"https://example.com/pull/{pr_number}",
                      "draft": False}, actor="r")
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": phase.value, "reason": ""}, actor="r")
    snap_mod.rebuild(cfg.home, key)


def test_check_merged_from_awaiting_ci(cfg):
    _create(cfg, "T-1", phase=Phase.AWAITING_CI)
    finalized = ops.check_merged(cfg, "T-1", "MERGED")
    assert finalized is True
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.DONE.value
    assert snap.pr_state == "merged"


def test_check_merged_from_implementing(cfg):
    _create(cfg, "T-1", phase=Phase.IMPLEMENTING)
    finalized = ops.check_merged(cfg, "T-1", "MERGED")
    assert finalized is True
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.DONE.value


def test_check_merged_from_in_review(cfg):
    _create(cfg, "T-1", phase=Phase.IN_REVIEW)
    finalized = ops.check_merged(cfg, "T-1", "MERGED")
    assert finalized is True
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.DONE.value


def test_check_merged_noop_for_open(cfg):
    _create(cfg, "T-1", phase=Phase.AWAITING_CI)
    finalized = ops.check_merged(cfg, "T-1", "OPEN")
    assert finalized is False
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_CI.value


def test_check_merged_noop_for_closed(cfg):
    _create(cfg, "T-1", phase=Phase.AWAITING_CI)
    finalized = ops.check_merged(cfg, "T-1", "CLOSED")
    assert finalized is False
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_CI.value


def test_check_merged_noop_if_already_done(cfg):
    _create(cfg, "T-1", phase=Phase.AWAITING_CI)
    ops.check_merged(cfg, "T-1", "MERGED")
    evs_before = len(event_log.read(cfg.home, "T-1"))
    # second call must be a no-op (ticket already done)
    ops.check_merged(cfg, "T-1", "MERGED")
    assert len(event_log.read(cfg.home, "T-1")) == evs_before


def test_check_merged_records_pr_updated_event(cfg):
    _create(cfg, "T-1", phase=Phase.AWAITING_CI)
    ops.check_merged(cfg, "T-1", "MERGED")
    evs = event_log.read(cfg.home, "T-1")
    pr_updated = [e for e in evs if e["type"] == "PrUpdated"]
    assert len(pr_updated) == 1
    assert pr_updated[0]["payload"]["merged"] is True


def test_check_merged_case_insensitive(cfg):
    _create(cfg, "T-1", phase=Phase.AWAITING_CI)
    finalized = ops.check_merged(cfg, "T-1", "merged")
    assert finalized is True
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.DONE.value


def test_check_merged_cli_merged(cfg):
    from maestro.cli import main
    _create(cfg, "T-1", phase=Phase.AWAITING_CI)
    rc = main(["--home", str(cfg.home), "check-merged", "T-1", "MERGED"])
    assert rc == 0
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.DONE.value


def test_check_merged_cli_open(cfg):
    from maestro.cli import main
    _create(cfg, "T-1", phase=Phase.AWAITING_CI)
    rc = main(["--home", str(cfg.home), "check-merged", "T-1", "OPEN"])
    assert rc == 0
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_CI.value
