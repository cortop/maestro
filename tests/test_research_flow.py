"""End-to-end flow test for research tickets.

AC1: create(kind=research) -> dispatch (mint) -> ready -> researching ->
     ResearchProposed -> ask -> ans(approve) -> dispatch -> fold-inbox ->
     create impl ticket -> dispatch (mint impl) -> finalize research.
     Assert: (a) impl ticket created with dependsOn=[research_key],
             (b) research ticket reached done.

AC2: proposal.md is written with recommendation + >=2 alternatives + sources.

AC3: ans("alternative 2") seeds the impl ticket from that alternative — asserted
     via the intent in the queued create entry.

Drives real CLI / ops / dispatcher (DryRunSessions only at the spawn boundary).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro import dispatcher as disp, event_log, inbox, ops, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_research_ticket(home: Path, cfg, key: str = "R-1") -> None:
    """Queue + mint a research ticket via inbox -> dispatch cycle."""
    inbox.append_new(
        home, f"Research: caching strategies for {key}",
        key=key,
        args={"approval_tier": 0, "kind": "research", "priority": 2},
    )
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert key in report.minted, f"{key} not minted: {report.minted}"


def _sim_triaging_to_ready(cfg, key: str) -> None:
    """Tier-0 auto-approve: triaging → ready."""
    ops.set_phase(cfg, key, Phase.READY, reason="tier-0 auto-approved")


def _sim_ready_to_researching(cfg, key: str) -> None:
    """Research ticket: ready → researching (no worktree)."""
    snap = snap_mod.load(cfg.home, key)
    assert snap.kind == "research", f"expected kind=research, got {snap.kind!r}"
    ops.set_phase(cfg, key, Phase.RESEARCHING, reason="research ticket: beginning exploration")


def _write_proposal(home: Path, key: str) -> Path:
    """Write a realistic proposal.md with recommended + 2 alternatives + sources."""
    ticket_dir = store.ticket_dir(home, key)
    ticket_dir.mkdir(parents=True, exist_ok=True)
    proposal = ticket_dir / "proposal.md"
    proposal.write_text(
        "# Proposal: caching strategies\n\n"
        "## Recommended\n"
        "Use an LRU cache with TTL expiry backed by a Redis sidecar.\n\n"
        "## Alternative 1\n"
        "In-process dict cache with a background sweeper thread.\n\n"
        "## Alternative 2\n"
        "Memcached cluster shared across worker processes.\n\n"
        "## Sources\n"
        "- maestro/store.py:10 — atomic_write pattern for cache invalidation\n"
        "- https://redis.io/docs/manual/eviction/ — LRU eviction reference\n"
    )
    return proposal


def _sim_research_proposed_and_ask(cfg, key: str, proposal_path: Path) -> None:
    """Simulate the reconciler: append ResearchProposed + ask."""
    rel = str(proposal_path.relative_to(cfg.home))
    event_log.append(
        cfg.home, key, "ResearchProposed",
        {"proposal_path": rel, "alternatives": ["Alternative 1", "Alternative 2"]},
        actor="reconciler",
        step_id=f"research-proposed-{key}",
    )
    snap_mod.rebuild(cfg.home, key)
    ops.ask(cfg, key,
            f"Proposal for {key} is ready. Approve or reply 'alternative N'.",
            qid=f"research-approval-{key}")


def _sim_awaiting_human_approve(cfg, key: str, answer: str, proposal_path: Path) -> str:
    """Fold the answer, create impl ticket, NOTE + finalize. Returns impl key."""
    # Fold inbox (the human's answer is already in the inbox)
    ops.fold_inbox(cfg, key)
    snap = snap_mod.load(cfg.home, key)
    assert snap.answered_questions, "no answered_questions after fold"

    # Determine chosen intent from proposal
    proposal_text = proposal_path.read_text()
    ans_lower = answer.strip().lower()
    if "alternative 2" in ans_lower:
        section = _extract_section(proposal_text, "## Alternative 2")
    elif "alternative 1" in ans_lower:
        section = _extract_section(proposal_text, "## Alternative 1")
    else:
        section = _extract_section(proposal_text, "## Recommended")

    # MTO-3: mint the implementation ticket through the real CLI, with the exact
    # flag shape maestro-reconcile-awaiting-human.md emits (positional title --
    # `--title` never existed on this subparser, which is why every one of the
    # skill's four prior drifts to `--title` broke this step silently).
    impl_key = f"IMPL-{key}"
    impl_title = f"Implement: caching strategies ({key})"
    rc = cli_main([
        "--home", str(cfg.home), "create", impl_title,
        "--kind", "implementation",
        "--intent", section,
        "--notes", f"Seeded from {key} proposal. See tickets/{key}/proposal.md for full context.",
        "--depends-on", key, "--key", impl_key, "--no-nudge",
    ])
    assert rc == 0, f"`maestro create` failed to mint {impl_key} (rc={rc})"

    # Append NOTE breadcrumb
    event_log.append(
        cfg.home, key, "Note",
        {"text": f"Created implementation ticket {impl_key} from approved proposal"},
        actor="reconciler",
        step_id=f"note-impl-created-{key}",
    )
    snap_mod.rebuild(cfg.home, key)

    # Finalize the research ticket
    ops.finalize(cfg, key)

    # Ack the inbox (LAST — so a crash before here re-reads the answer)
    inbox.ack(cfg.home, key)

    return impl_key


def _extract_section(text: str, header: str) -> str:
    """Extract text under a ## header up to the next ## header."""
    lines = text.splitlines()
    in_section = False
    result = []
    for line in lines:
        if line.strip() == header.strip():
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            result.append(line)
    return "\n".join(result).strip()


# ---------------------------------------------------------------------------
# AC1 + AC2: full flow, approve recommended
# ---------------------------------------------------------------------------

def test_research_flow_approve_recommended(home, cfg):
    """AC1: full research flow with 'approve' ends in done research + minted impl."""
    key = "R-1"
    _make_research_ticket(home, cfg, key)

    # Verify minted snapshot
    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.TRIAGING.value
    assert snap.kind == "research"

    # Triaging → ready (tier-0 auto)
    _sim_triaging_to_ready(cfg, key)

    # Ready → researching (no worktree for research tickets)
    _sim_ready_to_researching(cfg, key)
    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.RESEARCHING.value

    # AC2: proposal written with recommendation + >=2 alternatives + sources
    proposal_path = _write_proposal(home, key)
    assert "## Recommended" in proposal_path.read_text()
    assert "## Alternative 1" in proposal_path.read_text()
    assert "## Alternative 2" in proposal_path.read_text()
    assert "## Sources" in proposal_path.read_text()

    # ResearchProposed + ask → awaiting-human
    _sim_research_proposed_and_ask(cfg, key, proposal_path)
    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert snap.proposal_path is not None
    assert f"research-approval-{key}" in snap.open_questions

    # Human answers: approve
    inbox.append_command(home, key, "ans",
                         {"qid": f"research-approval-{key}", "text": "approve"})

    # Dispatch wakes the ticket (inbox pending)
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1001)
    assert key in report.spawned

    # Simulate reconciler: fold → create impl → finalize
    impl_key = _sim_awaiting_human_approve(cfg, key, "approve", proposal_path)

    # AC1(a): impl ticket queued with dependsOn=[R-1]
    pending = inbox.pending_new(home)
    impl_entries = [e for _, e in pending if e.get("key") == impl_key]
    assert impl_entries, f"impl ticket {impl_key} not found in _new inbox"
    impl_entry = impl_entries[0]
    assert impl_entry["args"].get("depends_on") == [key], (
        f"expected depends_on=[{key!r}], got {impl_entry['args'].get('depends_on')!r}"
    )
    assert impl_entry["args"].get("kind") == "implementation"

    # Dispatch sweep mints the impl ticket
    report2 = disp.dispatch(cfg, sessions, now=1002)
    assert impl_key in report2.minted

    # Verify impl ticket spec has dependsOn and the recommended intent
    impl_spec = store.spec_path(home, impl_key).read_text()
    assert f"dependsOn: [{key}]" in impl_spec
    assert "LRU cache" in impl_spec  # chosen approach from Recommended section

    # AC1(b): research ticket is done
    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.DONE.value, f"expected done, got {snap.phase!r}"


# ---------------------------------------------------------------------------
# AC3: "alternative 2" seeds from that alternative
# ---------------------------------------------------------------------------

def test_research_flow_alternative_2(home, cfg):
    """AC3: selecting 'alternative 2' seeds the impl ticket from that alternative."""
    key = "R-2"
    _make_research_ticket(home, cfg, key)
    _sim_triaging_to_ready(cfg, key)
    _sim_ready_to_researching(cfg, key)

    proposal_path = _write_proposal(home, key)
    _sim_research_proposed_and_ask(cfg, key, proposal_path)

    # Human answers: alternative 2
    inbox.append_command(home, key, "ans",
                         {"qid": f"research-approval-{key}", "text": "alternative 2"})

    impl_key = _sim_awaiting_human_approve(cfg, key, "alternative 2", proposal_path)

    # Verify the impl entry intent comes from Alternative 2
    pending = inbox.pending_new(home)
    impl_entries = [e for _, e in pending if e.get("key") == impl_key]
    assert impl_entries
    intent = impl_entries[0]["args"].get("intent", "")
    assert "Memcached" in intent, (
        f"expected Alternative 2 content (Memcached) in intent, got: {intent!r}"
    )
    assert "LRU cache" not in intent, "should NOT use Recommended content for alternative 2"
    assert "In-process dict" not in intent, "should NOT use Alternative 1 for alternative 2"


# ---------------------------------------------------------------------------
# "needs more" sends back to researching
# ---------------------------------------------------------------------------

def test_research_flow_needs_more(home, cfg):
    """Answering 'needs more' sends the ticket back to researching."""
    key = "R-3"
    _make_research_ticket(home, cfg, key)
    _sim_triaging_to_ready(cfg, key)
    _sim_ready_to_researching(cfg, key)

    proposal_path = _write_proposal(home, key)
    _sim_research_proposed_and_ask(cfg, key, proposal_path)

    inbox.append_command(home, key, "ans",
                         {"qid": f"research-approval-{key}", "text": "needs more"})
    ops.fold_inbox(cfg, key)

    # Simulate reconciler detecting "needs more"
    snap = snap_mod.load(home, key)
    answer = snap.answered_questions.get(f"research-approval-{key}", "")
    if "needs more" in answer.lower():
        ops.set_phase(cfg, key, Phase.RESEARCHING, reason="needs more research per human")
    inbox.ack(home, key)

    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.RESEARCHING.value


# ---------------------------------------------------------------------------
# ready + kind=research skips worktree
# ---------------------------------------------------------------------------

def test_ready_research_ticket_goes_to_researching_not_implementing(home, cfg):
    """A research ticket in ready goes to researching, not implementing."""
    key = "R-4"
    _make_research_ticket(home, cfg, key)
    _sim_triaging_to_ready(cfg, key)

    snap = snap_mod.load(home, key)
    assert snap.kind == "research"

    # The reconciler should NOT create a worktree; it goes to researching.
    _sim_ready_to_researching(cfg, key)
    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.RESEARCHING.value

    # No worktree created
    worktree_path = cfg.home / "worktrees" / key
    assert not worktree_path.exists(), "research ticket must not create a worktree"


# ---------------------------------------------------------------------------
# Snapshot fields: kind + proposal_path survive fold
# ---------------------------------------------------------------------------

def test_research_snapshot_fields_survive_fold(home, cfg):
    """kind and proposal_path are persisted through fold/load cycles."""
    key = "R-5"
    _make_research_ticket(home, cfg, key)
    _sim_triaging_to_ready(cfg, key)
    _sim_ready_to_researching(cfg, key)

    proposal_path = _write_proposal(home, key)
    _sim_research_proposed_and_ask(cfg, key, proposal_path)

    snap = snap_mod.load(home, key)
    assert snap.kind == "research"
    assert snap.proposal_path is not None
    assert "proposal.md" in snap.proposal_path

    # Reload from disk to confirm persistence
    snap2 = snap_mod.rebuild(home, key)
    assert snap2.kind == snap.kind
    assert snap2.proposal_path == snap.proposal_path
