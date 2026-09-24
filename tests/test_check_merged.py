"""ops.check_merged and the check-merged CLI command finalize a ticket from any phase.

T-126 extends it: a ticket with an approved PR-split stack (`pr_stack`, from
`PrOpened` events carrying "stack" metadata) advances `pr_number`/`pr_url` to
the next entry each time the CURRENTLY-tracked one merges, finalizing only
once the LAST entry does.
"""
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


def _create_stack(cfg, key, *, phase=Phase.AWAITING_CI, n=3):
    """A ticket with an *n*-entry approved split, mirroring exactly what the
    implementing skill's "Approved split" block appends: one `PrOpened` per
    entry, index 0-based, each carrying its own `stack` sub-payload. PR
    numbers are 100, 101, 102, ... in stack order."""
    store.atomic_write(store.spec_path(cfg.home, key), f"# {key}\napproval_tier: 1\n")
    event_log.append(cfg.home, key, "TicketCreated", {"title": key}, actor="d")
    for i in range(n):
        pr = 100 + i
        base = "main" if i == 0 else f"maestro/{key}-{i}"
        event_log.append(cfg.home, key, "PrOpened", {
            "number": pr, "url": f"https://example.com/pull/{pr}", "draft": True,
            "stack": {"index": i, "total": n, "branch": f"maestro/{key}-{i+1}", "base": base},
        }, actor="r")
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


# ---------------------------------------------------------------------------
# T-126: an approved PR-split stack advances pr_number/pr_url down the stack
# as each entry merges, finalizing only once the last one does.
# ---------------------------------------------------------------------------

def test_check_merged_stack_advances_to_next_entry_without_finalizing(cfg):
    _create_stack(cfg, "T-1", n=3)
    assert snap_mod.load(cfg.home, "T-1").pr_number == 100

    changed = ops.check_merged(cfg, "T-1", "MERGED")
    assert changed is True
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.AWAITING_CI.value  # not finalized yet
    assert snap.pr_number == 101
    assert snap.pr_url == "https://example.com/pull/101"
    entry0 = next(e for e in snap.pr_stack if e["index"] == 0)
    entry1 = next(e for e in snap.pr_stack if e["index"] == 1)
    assert entry0["merged"] is True
    assert entry1["merged"] is False


def test_check_merged_stack_cascades_through_every_entry_then_finalizes(cfg):
    _create_stack(cfg, "T-1", n=3)

    assert ops.check_merged(cfg, "T-1", "MERGED") is True  # entry 0 -> 1
    assert snap_mod.load(cfg.home, "T-1").pr_number == 101

    assert ops.check_merged(cfg, "T-1", "MERGED") is True  # entry 1 -> 2
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.pr_number == 102
    assert snap.phase == Phase.AWAITING_CI.value

    finalized = ops.check_merged(cfg, "T-1", "MERGED")  # entry 2 (last) -> done
    assert finalized is True
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.DONE.value
    assert snap.pr_state == "merged"
    assert all(e["merged"] for e in snap.pr_stack)


def test_check_merged_stack_open_poll_is_a_noop(cfg):
    _create_stack(cfg, "T-1", n=3)
    changed = ops.check_merged(cfg, "T-1", "OPEN")
    assert changed is False
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.pr_number == 100
    assert not any(e["merged"] for e in snap.pr_stack)


def test_check_merged_stack_entry_merge_scoped_pr_updated_event(cfg):
    _create_stack(cfg, "T-1", n=2)
    ops.check_merged(cfg, "T-1", "MERGED")
    evs = event_log.read(cfg.home, "T-1")
    pr_updated = [e for e in evs if e["type"] == "PrUpdated"]
    assert len(pr_updated) == 1
    assert pr_updated[0]["payload"] == {"merged": True, "stack_index": 0}
    pr_opened = [e for e in evs if e["type"] == "PrOpened"]
    # the 2 original stack-opening events plus the one advance-open
    assert len(pr_opened) == 3
    assert pr_opened[-1]["payload"]["number"] == 101


def test_pr_stack_empty_for_a_non_split_ticket(cfg):
    """AC4: threshold-0/under-threshold tickets never populate pr_stack --
    byte-identical to before this ticket existed."""
    _create(cfg, "T-1", phase=Phase.AWAITING_CI)
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.pr_stack == []
    finalized = ops.check_merged(cfg, "T-1", "MERGED")
    assert finalized is True
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.DONE.value
