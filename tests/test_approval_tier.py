"""AD-1: approval_tier as a real precondition, not just a mint-time label.

Two enforcement points, per the spec: (1) the tool surface a spawned reconciler
gets (`--disallowedTools`), and (2) whether a tier-2 ticket is even due for the
`implementing` phase before a human runs `maestro approve`.
"""
from maestro import cli, dispatcher as disp, event_log, ops, projection, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import seed_ticket


def _spawned_by_key(sessions):
    return {k: (m, e, d) for k, _p, _c, m, e, d in sessions.spawned}


def _seed(cfg, key, title, *, phase, tier):
    """`seed_ticket` + a real `observe_spec` so the snapshot's spec_hash matches
    the file on disk -- otherwise `is_due`'s spec-changed check (which runs
    ahead of the tier gate) would always fire first and mask it."""
    seed_ticket(cfg.home, key, title, phase=phase, tier=tier)
    ops.observe_spec(cfg, key)
    return snap_mod.load(cfg.home, key)


# --- AC1: parse_spec_overrides / spec_tier ----------------------------------

def test_spec_tier_reads_approval_tier_from_spec(home):
    seed_ticket(home, "T-1", "x", phase="ready", tier=2)
    assert disp.spec_tier(home, "T-1") == 2


def test_spec_tier_defaults_to_1_when_missing_or_malformed(home):
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: not-a-number\n")
    assert disp.spec_tier(home, "T-1") == 1
    assert disp.spec_tier(home, "NOPE") == 1  # no spec file at all


# --- AC2: tier>=1 spawn gets a --disallowedTools deny entry for gh pr merge --

def test_tier0_spawn_has_no_denylist(home, cfg):
    _seed(cfg, "T-0", "auto-approved", phase="ready", tier=0)
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert "T-0" in report.spawned
    _model, _effort, denylist = _spawned_by_key(sessions)["T-0"]
    assert denylist == []


def test_tier1_spawn_denies_gh_pr_merge(home, cfg):
    _seed(cfg, "T-1", "needs a human eye", phase="ready", tier=1)
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert "T-1" in report.spawned
    _model, _effort, denylist = _spawned_by_key(sessions)["T-1"]
    assert "Bash(gh pr merge:*)" in denylist


# --- AC3 + AC4 + AC5: tier-2 implementing gate + `maestro approve` ----------

def test_tier2_implementing_blocked_until_approved_then_unblocked(home, cfg):
    """Real-CLI flow over a temp home: mint a tier-2 ticket already in
    `implementing`, a dry-run sweep shows it's not due (needs-approval) and its
    tool surface would still deny `gh pr merge`, `maestro approve` appends the
    unblocking event, and the very next dry-run sweep spawns it -- with the
    same denylist still in its argv."""
    from maestro import cli

    key = "T-2"
    snap = _seed(cfg, key, "risky change", phase="implementing", tier=2)

    # Not due: is_due directly names the reason...
    res = disp.is_due(snap, inbox_pending=False, current_spec_hash=snap.spec_hash,
                      now=1000, tier=disp.spec_tier(home, key))
    assert res == disp.DueResult(False, "needs-approval")

    # ...and a real dry-run sweep never spawns it.
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert key not in report.spawned
    assert key not in [k for k, _r in report.due]

    # `maestro approve` (real CLI) clears the gate, idempotently.
    rc = cli.main(["--home", str(home), "approve", key, "--no-nudge"])
    assert rc == 0
    assert snap_mod.load(home, key).approved is True
    approved_events = [e for e in event_log.read(home, key) if e["type"] == "Approved"]
    assert len(approved_events) == 1
    rc = cli.main(["--home", str(home), "approve", key, "--no-nudge"])  # repeat is a no-op
    assert rc == 0
    approved_events = [e for e in event_log.read(home, key) if e["type"] == "Approved"]
    assert len(approved_events) == 1

    # The next real dry-run sweep now spawns it, denylist intact (tier 2 >= 1).
    report2 = disp.dispatch(cfg, sessions, now=2000)
    assert key in report2.spawned
    _model, _effort, denylist = _spawned_by_key(sessions)[key]
    assert "Bash(gh pr merge:*)" in denylist


def test_tier1_implementing_is_not_gated_by_approval(home, cfg):
    """The tier-2 gate is specific to tier>=2 -- a tier-1 ticket in implementing
    is due without ever calling `maestro approve` (only the tool denylist
    applies to it)."""
    _seed(cfg, "T-1", "ordinary change", phase="implementing", tier=1)
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert "T-1" in report.spawned


def test_approval_persists_across_reentry_into_implementing(home, cfg):
    """Once approved, a tier-2 ticket that cycles back into implementing (e.g.
    a CI-failure retry) does not need re-approval -- `approved` survives the
    PhaseChanged that resets other per-visit snapshot fields."""
    key = "T-2"
    _seed(cfg, key, "risky change", phase="implementing", tier=2)
    ops.approve(cfg, key)

    # Route back through awaiting-ci and into implementing again.
    ops.set_phase(cfg, key, Phase.AWAITING_CI)
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="ci failed, retry")

    snap = snap_mod.load(home, key)
    assert snap.approved is True
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert key in report.spawned


# --- GA-18: Tier column rendered from spec_tier, not the dead snapshot field ---

def test_tier_column_renders_from_spec_tier_end_to_end(home, cfg):
    """QA per CLAUDE.md, real CLI over a temp home: tickets with approval_tier
    0, 2, absent, and malformed all render the correct Tier in both
    `ticket_rows` and WORKSTATE.md after a real sweep + a real `project`
    regeneration -- and the tier-2 ticket's gate behavior (still needs-approval
    before `maestro approve`, still spawned with the `gh pr merge` denylist
    once approved) is unchanged. Mocks only the `claude -p` spawn (DryRunSessions)."""
    seed_ticket(home, "Z-0", "auto-approved", phase="ready", tier=0)
    ops.observe_spec(cfg, "Z-0")
    seed_ticket(home, "Z-2", "risky change", phase="implementing", tier=2)
    ops.observe_spec(cfg, "Z-2")
    seed_ticket(home, "Z-A", "no tier field")
    store.atomic_write(store.spec_path(home, "Z-A"), "# Z-A\n\n## Intent\nno tier field\n")
    ops.observe_spec(cfg, "Z-A")
    seed_ticket(home, "Z-M", "malformed tier")
    store.atomic_write(store.spec_path(home, "Z-M"), "# Z-M\napproval_tier: not-a-number\n")
    ops.observe_spec(cfg, "Z-M")

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert "Z-0" in report.spawned
    assert "Z-2" not in report.spawned  # tier-2 implementing: needs-approval

    rc = cli.main(["--home", str(home), "project"])
    assert rc == 0

    rows = {r[0]: r[5] for r in projection.ticket_rows(home)}
    assert rows["Z-0"] == "0"   # falsy-0 bug fixed: not "—"
    assert rows["Z-2"] == "2"
    assert rows["Z-A"] == "1"   # missing approval_tier -> documented fallback
    assert rows["Z-M"] == "1"   # malformed approval_tier -> documented fallback

    workstate = (home / "derived" / "WORKSTATE.md").read_text()
    for key, expected in (("Z-0", "0"), ("Z-2", "2"), ("Z-A", "1"), ("Z-M", "1")):
        line = next(l for l in workstate.splitlines() if l.startswith(f"| {key} "))
        cells = [c.strip() for c in line.split("|")]
        assert cells[6] == expected, f"{key}: expected tier {expected!r}, row was {line!r}"

    # Gate behavior is unchanged by this rendering-only change.
    rc = cli.main(["--home", str(home), "approve", "Z-2", "--no-nudge"])
    assert rc == 0
    from dataclasses import replace
    # A wide concurrency ceiling so the other 3 seeded tickets never crowd Z-2
    # out of this sweep -- capacity is not what this assertion is about.
    wide_cfg = replace(cfg, max_concurrency=10)
    report2 = disp.dispatch(wide_cfg, sessions, now=2000)
    assert "Z-2" in report2.spawned
    spawned_by_key = {k: (m, e, d) for k, _p, _c, m, e, d in sessions.spawned}
    assert "Bash(gh pr merge:*)" in spawned_by_key["Z-2"][2]
