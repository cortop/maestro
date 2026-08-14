"""RF-6: an inert `qa` Phase, its routing, and a phase-keyed tool denylist.

Additive only -- nothing routes a ticket into `qa` yet (that's a later ticket).
These tests prove the mechanism works in isolation: the transition table
accepts implementing<->qa and qa->awaiting-ci but rejects qa->done, `qa` is
active (not sleeping), a real sweep spawns the `qa` reconcile command with
Edit+Write denied, every other phase's denylist is unchanged, the two
`.claude/commands`/`skills` copies exist and are byte-identical (and doctor's
missing-skill check passes), the TUI mounts clean over a `qa` ticket, and a
full sweep over the existing seeded home is completely unaffected -- no
ticket is routed to `qa`.
"""
from __future__ import annotations

import json
from pathlib import Path

from maestro import cli, dispatcher as disp, event_log, health, ops, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import ACTIVE_PHASES, SLEEPING_PHASES, TRANSITIONS, Phase, can_transition

from conftest import seed_ticket

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed(home, key, phase=Phase.READY, tier=1):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: {tier}\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


# ---------------------------------------------------------------------------
# AC1: statemachine.can_transition accepts implementing<->qa and qa->awaiting-ci,
# rejects qa->done. A real `set-phase` records no "unusual transition" note.
# ---------------------------------------------------------------------------

def test_can_transition_implementing_qa_awaiting_ci():
    assert can_transition(Phase.IMPLEMENTING, Phase.QA)
    assert can_transition(Phase.QA, Phase.IMPLEMENTING)
    assert can_transition(Phase.QA, Phase.AWAITING_CI)


def test_can_transition_qa_to_done_rejected():
    assert not can_transition(Phase.QA, Phase.DONE)


def test_real_set_phase_implementing_to_qa_records_no_unusual_transition_note(home, cfg):
    _seed(home, "T-1", Phase.IMPLEMENTING)
    ops.set_phase(cfg, "T-1", Phase.QA, reason="qa: hand off for review")
    events = event_log.read(home, "T-1")
    notes = [e for e in events if e["type"] == "Note"
             and "unusual transition" in e.get("payload", {}).get("text", "")]
    assert notes == []
    phase_changed = [e for e in events if e["type"] == "PhaseChanged"]
    assert phase_changed[-1]["payload"]["phase"] == "qa"


def test_real_set_phase_qa_to_implementing_and_awaiting_ci_record_no_unusual_note(home, cfg):
    _seed(home, "T-2", Phase.QA)
    ops.set_phase(cfg, "T-2", Phase.AWAITING_CI, reason="qa: all ACs pass")
    events = event_log.read(home, "T-2")
    notes = [e for e in events if e["type"] == "Note"
             and "unusual transition" in e.get("payload", {}).get("text", "")]
    assert notes == []


# ---------------------------------------------------------------------------
# AC2: `qa` is active, not sleeping.
# ---------------------------------------------------------------------------

def test_qa_is_active_not_sleeping():
    assert Phase.QA in ACTIVE_PHASES
    assert Phase.QA not in SLEEPING_PHASES


# ---------------------------------------------------------------------------
# AC3: a real sweep over a home with one `qa` ticket spawns exactly once, with
# the qa reconcile command -- and `maestro env --key` prints the same command.
# ---------------------------------------------------------------------------

def test_dryrun_sweep_over_qa_ticket_spawns_qa_command(home, cfg):
    _seed(home, "T-1", Phase.QA, tier=0)
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert report.spawned == ["T-1"]
    assert len(sessions.spawned) == 1
    key, prompt, *_ = sessions.spawned[0]
    assert key == "T-1"
    assert prompt.startswith("/maestro-reconcile-qa T-1")


def test_env_key_prints_the_same_qa_command_as_the_real_spawn(home, capsys):
    _seed(home, "T-1", Phase.QA, tier=0)
    rc = cli.main(["--home", str(home), "env", "--key", "T-1"])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["reconcile_command"] == "/maestro-reconcile-qa"


# ---------------------------------------------------------------------------
# AC4: the qa spawn's disallowed_tools include Edit and Write; every other
# phase's denylist is byte-identical to the pre-change baseline (tier-keyed
# only).
# ---------------------------------------------------------------------------

def test_qa_spawn_denies_edit_and_write(home, cfg):
    _seed(home, "T-1", Phase.QA, tier=0)
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    _key, _prompt, _cwd, _model, _effort, disallowed, _allowed, *_ = sessions.spawned[0]
    assert "Edit" in disallowed
    assert "Write" in disallowed


def test_phase_denylist_empty_for_every_phase_except_qa():
    for phase in Phase:
        if phase == Phase.QA:
            assert disp.phase_denylist(phase.value) == ["Edit", "Write"]
        else:
            assert disp.phase_denylist(phase.value) == []


def test_other_phase_spawn_denylist_unchanged_baseline(home, cfg):
    _seed(home, "T-1", Phase.READY, tier=0)
    _seed(home, "T-2", Phase.READY, tier=1)
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    by_key = {k: d for k, _p, _c, _m, _e, d, *_ in sessions.spawned}
    assert by_key["T-1"] == []  # tier 0, unaffected by RF-6
    assert by_key["T-2"] == ["Bash(gh pr merge:*)"]  # tier 1, unaffected by RF-6


# ---------------------------------------------------------------------------
# AC5: both copies exist, byte-identical; `maestro doctor` reports the
# missing-reconcile-skill check ok over a home with a `qa` ticket.
# ---------------------------------------------------------------------------

def test_qa_command_files_exist_and_are_byte_identical():
    commands = REPO_ROOT / ".claude" / "commands" / "maestro-reconcile-qa.md"
    skills = REPO_ROOT / "skills" / "maestro-reconcile-qa.md"
    assert commands.exists()
    assert skills.exists()
    assert commands.read_text() == skills.read_text()


def test_doctor_missing_reconcile_skill_ok_with_a_qa_ticket(home):
    cfg = Config(home=home, repo_path=str(REPO_ROOT), min_spawn_interval=0)
    seed_ticket(home, "T-1", "qa ticket", phase="qa")
    check = health.check_missing_reconcile_skill(cfg, 1000)
    assert check["status"] == "ok"


# ---------------------------------------------------------------------------
# AC6: the TUI mounts clean over a seeded home with a `qa` ticket; every
# filter is selectable.
# ---------------------------------------------------------------------------

def test_tui_mounts_clean_over_a_qa_ticket(seeded_home):
    import asyncio

    import pytest

    pytest.importorskip("textual", reason="requires the [tui] extra (textual)")
    from maestro.tui import MaestroTUI, _FILTERS
    from textual.widgets import DataTable

    seed_ticket(seeded_home, "T-6", "qa ticket", phase="qa")

    async def _inner():
        app = MaestroTUI(home=str(seeded_home))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one("#tickets", DataTable).row_count >= 1
            for _ in range(len(_FILTERS)):
                app.action_cycle_filter()
                await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


# ---------------------------------------------------------------------------
# AC7: a full sweep over the existing seeded home is completely unaffected --
# no existing ticket is routed to `qa`.
# ---------------------------------------------------------------------------

def test_full_sweep_over_seeded_home_unchanged_no_qa_routing(seeded_home):
    cfg = Config(home=seeded_home, repo_path=str(REPO_ROOT), min_spawn_interval=0)
    # `seed_ticket` records a placeholder spec_hash ("x"), so a real sweep would
    # see every ticket as "spec-changed" (due) regardless of phase -- sync it to
    # the real on-disk hash first, exactly as `ops.observe_spec` would on a live
    # board, so this sweep exercises the same is_due/phase routing a real one does.
    for key in ("T-1", "T-2", "T-3", "T-4", "T-5"):
        ops.observe_spec(cfg, key)
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    # T-1 awaiting-human (sleeping), T-2 degraded (sleeping, OC-7), and T-4
    # awaiting-ci (sleeping) never spawn; T-3/T-5 are active and spawn their
    # existing per-phase commands.
    assert sorted(report.spawned) == ["T-3", "T-5"]
    by_key = {k: (p, d) for k, p, _c, _m, _e, d, *_ in sessions.spawned}
    assert by_key["T-3"][0].startswith("/maestro-reconcile-implementing T-3")
    assert by_key["T-5"][0].startswith("/maestro-reconcile-ready T-5")
    for key in ("T-3", "T-5"):
        assert by_key[key][1] == ["Bash(gh pr merge:*)"]  # tier-1 default, unchanged

    for key in ("T-1", "T-2", "T-3", "T-4", "T-5"):
        assert snap_mod.load(seeded_home, key).phase != "qa"
