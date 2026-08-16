"""T-34/RF-5: the interlock -- until a bypass-resistant equivalent of the Claude
Code destructive-command hook exists for a backend, one predicate refuses to
spawn into it at all. Asserted from a REAL dispatch() sweep (not a mock of
dispatch itself, per this repo's QA convention) -- only DryRunSessions stands in
for the external `claude`/other-CLI spawn boundary.
"""
from __future__ import annotations

from maestro import dispatcher as disp
from maestro import event_log, gates, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _seed_ready_ticket(home, key):
    store.atomic_write(store.spec_path(home, key),
                        f"# {key}\napproval_tier: 0\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.READY.value, "reason": ""}, actor="d")
    return snap_mod.rebuild(home, key)


def test_backend_interlock_reason_is_none_for_claude_skill(home):
    cfg = Config(home=home)  # default providers.implementer == "claude_skill"
    assert gates.backend_interlock_reason(cfg) is None


def test_backend_interlock_reason_fires_for_a_non_claude_implementer(home):
    cfg = Config(home=home, providers={"tracker": "none", "vcs": "none",
                                        "fetcher": "none", "implementer": "opencode"})
    reason = gates.backend_interlock_reason(cfg)
    assert reason is not None
    assert "opencode" in reason
    assert "bypass-resistant" in reason


def test_real_sweep_refuses_to_spawn_into_a_non_claude_backend(home):
    """The AC's own bar: a real dispatch() sweep, no spawn recorded, a visible
    open question, and no attempts-ledger spend."""
    cfg = Config(home=home, providers={"tracker": "none", "vcs": "none",
                                        "fetcher": "none", "implementer": "opencode"})
    _seed_ready_ticket(home, "X-1")

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    # No spawn recorded, anywhere.
    assert "X-1" not in report.spawned
    assert sessions.spawned == []

    # A visible open question.
    snap = snap_mod.load(home, "X-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert snap.open_questions, "expected a visible open question"
    question_text = next(iter(snap.open_questions.values()))
    assert "opencode" in question_text
    assert "bypass-resistant" in question_text

    # No attempts spend: the spawn-attempts ledger was never written for this key.
    attempts_path = disp._spawn_attempts_path(home)
    attempts = store.read_json(attempts_path, {}) or {}
    assert "X-1" not in attempts


def test_dry_run_preview_reflects_the_interlock_too(home):
    """A read-only `make dry` preview must not claim `would_spawn` for a key
    the next real sweep would actually refuse -- see dispatcher.dispatch's
    dry_run branch, which now reads the same interlock_reason as the real
    spawn loop instead of unconditionally reporting would_spawn."""
    cfg = Config(home=home, providers={"tracker": "none", "vcs": "none",
                                        "fetcher": "none", "implementer": "opencode"})
    _seed_ready_ticket(home, "X-3")

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000, dry_run=True)

    assert "X-3" not in report.spawned
    decision = disp.key_decisions(home, "X-3", tail=1)[0]
    assert decision["outcome"] == "would_ask_backend_interlocked"

    # Still strictly read-only: no phase change, no question asked.
    snap = snap_mod.load(home, "X-3")
    assert snap.phase == Phase.READY.value
    assert not snap.open_questions


def test_claude_skill_backend_is_unaffected_by_the_interlock(home):
    """Sanity: the default (and every existing test's) backend keeps spawning
    exactly as before -- the interlock only ever fires for a non-claude_skill
    implementer."""
    cfg = Config(home=home)
    _seed_ready_ticket(home, "X-2")
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert "X-2" in report.spawned


def test_backend_interlock_reason_is_none_for_pi(home):
    """T-59 (PI-7): the last commit of that ticket -- `pi` finally has a
    bypass-resistant destructive-command guard wired in (`maestro.pi_guard`,
    proven live in `tests/test_pi_guard.py` including a real pi run), so the
    interlock no longer refuses it."""
    cfg = Config(home=home, providers={"tracker": "none", "vcs": "none",
                                        "fetcher": "none", "implementer": "pi"})
    assert gates.backend_interlock_reason(cfg) is None


def test_real_sweep_with_a_pi_implementer_spawns_normally(home):
    """The AC's own bar, mirrored from
    `test_real_sweep_refuses_to_spawn_into_a_non_claude_backend`: a real
    dispatch() sweep with `providers.implementer == "pi"` spawns exactly like
    the claude_skill default -- no interlock refusal, no open question."""
    cfg = Config(home=home, providers={"tracker": "none", "vcs": "none",
                                        "fetcher": "none", "implementer": "pi"})
    _seed_ready_ticket(home, "X-4")

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    assert "X-4" in report.spawned
    snap = snap_mod.load(home, "X-4")
    assert not snap.open_questions
