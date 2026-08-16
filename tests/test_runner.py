"""RF-2: resolve a per-key runner at spawn time (spec `runner:`/`runner_model:`
front-matter -> board config, claude-only outside the implementation spawn, fail
closed on an unregistered name via `ops.ask`) and route every `sessions.spawn`
call through one `RoutingSessions` -- "claude" is the only registered runner as
of this ticket, so every test here proves the seam lands byte-identical by
default, not that a second backend actually works (there isn't one yet).
"""
from __future__ import annotations

from pathlib import Path

from maestro import config as config_mod
from maestro import dispatcher as disp
from maestro import event_log, projection, snapshot as snap_mod, store
from maestro.cli import _AGENT_TOOL_VERBS
from maestro.config import Config
from maestro.gates import parse_spec_overrides
from maestro.sessions import DryRunSessions, RoutingSessions
from maestro.statemachine import Phase


def test_config_load_parses_runner_and_runner_model(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nrunner = "opencode"\n'
        'runner_model = "qwen3-coder:30b"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.runner == "opencode"
    assert cfg.runner_model == "qwen3-coder:30b"


def test_config_load_defaults_runner_to_claude(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.runner == "claude"
    assert cfg.runner_model is None


def _seed(home, key, *, phase=Phase.IMPLEMENTING, runner=None, runner_model=None,
          approval_tier=0):
    extra = ""
    if runner:
        extra += f"runner: {runner}\n"
    if runner_model:
        extra += f"runner_model: {runner_model}\n"
    spec = f"# {key}\napproval_tier: {approval_tier}\n{extra}dependsOn: []\n"
    store.atomic_write(store.spec_path(home, key), spec)
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


# --- AC1: gates.parse_spec_overrides carries runner/runner_model verbatim ---

def test_parse_spec_overrides_returns_runner_and_runner_model_when_present():
    spec = ("# T-1\napproval_tier: 0\nrunner: opencode\n"
            "runner_model: qwen3-coder:30b\n\n## Intent\nx\n")
    overrides = parse_spec_overrides(spec)
    assert overrides["runner"] == "opencode"
    assert overrides["runner_model"] == "qwen3-coder:30b"


def test_parse_spec_overrides_omits_runner_when_absent():
    overrides = parse_spec_overrides("# T-1\napproval_tier: 0\n\n## Intent\nx\n")
    assert "runner" not in overrides
    assert "runner_model" not in overrides


def test_parse_spec_overrides_returns_unrecognised_runner_verbatim_no_normalisation():
    spec = "# T-1\napproval_tier: 0\nrunner: Some-Weird_Value!!\n\n## Intent\nx\n"
    overrides = parse_spec_overrides(spec)
    assert overrides["runner"] == "Some-Weird_Value!!"  # untouched, no lowercasing/stripping


# --- AC2: no spec carries `runner:` -> byte-identical default ---------------

def test_no_runner_override_sweep_is_byte_identical_baseline(home, cfg):
    """A plain ticket with no `runner:` line -- every existing home, today --
    spawns with the exact pre-existing tuple elements RF-2 found (only a new
    `runner` element is appended), and touches none of the generated
    projections: `runner`/`runner_model` are spec-only (never the TicketCreated
    payload or the snapshot fold, per the ticket's Notes), so nothing about
    them can leak into a snapshot, WORKSTATE.md, or NEEDS-YOU.md."""
    _seed(home, "T-1", phase=Phase.READY)
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert report.spawned == ["T-1"]

    key, prompt, cwd, model, effort, disallowed, allowed, overlay, runner, runner_model = (
        sessions.spawned[0])
    # RB-16: disallowed/allowed are now phase-scoped (ready's own narrowed verb
    # grant/denylist), not the flat "deny only gh pr merge / allow nothing extra"
    # pair every phase shared before this ticket.
    assert (key, prompt, cwd, model, effort, disallowed, allowed, overlay) == (
        "T-1", "/maestro-reconcile-ready T-1", str(home), "sonnet", None,
        ["Bash(gh pr merge:*)"] + disp.phase_verb_denylist(Phase.READY.value),
        disp.phase_verb_grant(Phase.READY.value), {})
    assert runner == "claude"
    assert runner_model is None

    snap = snap_mod.load(home, "T-1")
    snap_dict = snap.to_dict()
    assert "runner" not in snap_dict
    assert "runner_model" not in snap_dict

    projection.write(home)
    workstate = (home / "derived" / "WORKSTATE.md").read_text(encoding="utf-8")
    needs_you = (home / "derived" / "NEEDS-YOU.md").read_text(encoding="utf-8")
    assert "runner" not in workstate.lower()
    assert "runner" not in needs_you.lower()


# --- AC3: dispatcher.resolve_runner ------------------------------------------

def test_resolve_runner_forces_claude_outside_implementing_even_if_spec_says_otherwise(home):
    cfg = Config(home=home)
    _seed(home, "T-1", phase=Phase.TRIAGING, runner="opencode")
    assert disp.resolve_runner(cfg, "T-1", Phase.TRIAGING.value) == ("claude", None)


def test_resolve_runner_falls_back_to_board_config_default_when_no_spec_override(home):
    """Two precedence levels only -- spec -> board config, matching model/effort."""
    cfg = Config(home=home, runner="claude", runner_model="pinned-tag")
    _seed(home, "T-1", phase=Phase.IMPLEMENTING)  # no runner:/runner_model: override
    assert disp.resolve_runner(cfg, "T-1", Phase.IMPLEMENTING.value) == ("claude", "pinned-tag")


def test_resolve_runner_missing_spec_falls_back_to_claude_no_exception(home):
    cfg = Config(home=home)
    assert disp.resolve_runner(cfg, "GHOST", Phase.IMPLEMENTING.value) == ("claude", None)


def test_resolve_runner_empty_spec_falls_back_to_claude_no_exception(home):
    cfg = Config(home=home)
    store.atomic_write(store.spec_path(home, "T-1"), "")
    assert disp.resolve_runner(cfg, "T-1", Phase.IMPLEMENTING.value) == ("claude", None)


def test_resolve_runner_malformed_runner_line_falls_back_to_claude_no_exception(home):
    cfg = Config(home=home)
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 0\nrunner:\n\n## Intent\nx\n")
    assert disp.resolve_runner(cfg, "T-1", Phase.IMPLEMENTING.value) == ("claude", None)


def test_dispatch_forces_claude_for_triaging_ticket_with_opencode_spec_override(home, cfg):
    """AC3, proven through a real sweep's recorded tuple, not just the pure
    function call above."""
    _seed(home, "T-1", phase=Phase.TRIAGING, runner="opencode")
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert "T-1" in report.spawned
    runner_by_key = {s[0]: s[-2] for s in sessions.spawned}  # -1 is `runner_model` (OC-4)
    assert runner_by_key["T-1"] == "claude"


# --- AC4: a spec edit changes the next resolve_runner call, no event append -

def test_resolve_runner_reflects_spec_edit_with_no_intervening_event_append(home):
    cfg = Config(home=home)
    _seed(home, "T-1", phase=Phase.IMPLEMENTING, runner="claude")
    assert disp.resolve_runner(cfg, "T-1", Phase.IMPLEMENTING.value) == ("claude", None)
    events_before = event_log.read(home, "T-1")

    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 0\nrunner: opencode\ndependsOn: []\n")
    runner, _ = disp.resolve_runner(cfg, "T-1", Phase.IMPLEMENTING.value)
    assert runner == "opencode"
    assert event_log.read(home, "T-1") == events_before


# --- AC5: an unregistered runner fails closed -------------------------------

def test_dispatch_unregistered_runner_asks_and_skips_spawn_sibling_still_spawns(home, cfg):
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner="rogue-runner")
    _seed(home, "OK-1", phase=Phase.READY)
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    assert "BAD-1" not in report.spawned
    assert "OK-1" in report.spawned
    assert "BAD-1" not in {s[0] for s in sessions.spawned}

    snap = snap_mod.load(home, "BAD-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert snap.open_questions

    events = event_log.read(home, "BAD-1")
    assert not any(e["type"] == "Failed" for e in events)

    attempts = store.read_json(home / "derived" / ".spawn_attempts.json", {}) or {}
    assert "BAD-1" not in attempts


def test_dispatch_unregistered_runner_never_spawns_claude_as_a_fallback(home, cfg):
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner="rogue-runner")
    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    assert sessions.spawned == []


# --- AC6: RoutingSessions ----------------------------------------------------

def test_routing_sessions_routes_spawn_to_the_delegate_matching_runner_name():
    claude_delegate = DryRunSessions()
    opencode_delegate = DryRunSessions()
    routing = RoutingSessions({"claude": claude_delegate, "opencode": opencode_delegate})

    routing.spawn("T-1", "/maestro-reconcile-implementing", Path("/tmp"), runner="claude")
    routing.spawn("T-2", "/maestro-reconcile-implementing", Path("/tmp"), runner="opencode")

    assert [s[0] for s in claude_delegate.spawned] == ["T-1"]
    assert [s[0] for s in opencode_delegate.spawned] == ["T-2"]


def test_routing_sessions_defaults_to_claude_when_runner_not_given():
    claude_delegate = DryRunSessions()
    routing = RoutingSessions({"claude": claude_delegate})
    routing.spawn("T-1", "/maestro-reconcile-implementing", Path("/tmp"))
    assert [s[0] for s in claude_delegate.spawned] == ["T-1"]


def test_routing_sessions_list_active_asks_a_single_delegate_not_every_one():
    calls = []

    class _CountingDryRun(DryRunSessions):
        def list_active(self):
            calls.append(1)
            return super().list_active()

    d1 = _CountingDryRun(active={"T-1"})
    d2 = _CountingDryRun(active={"T-2"})
    routing = RoutingSessions({"claude": d1, "opencode": d2})
    assert routing.list_active() == {"T-1"}
    assert len(calls) == 1


def test_routing_sessions_spawn_to_unregistered_runner_raises_clearly():
    routing = RoutingSessions({"claude": DryRunSessions()})
    try:
        routing.spawn("T-1", "/cmd", Path("/tmp"), runner="ghost")
    except store.MaestroError as e:
        assert "ghost" in str(e)
    else:
        raise AssertionError("expected a MaestroError")


def test_routing_sessions_satisfies_session_manager_via_a_real_sweep(home, cfg):
    """RoutingSessions works as a real `dispatch()` sessions argument, exactly
    like the bare ClaudeCliSessions it replaces at both cli.py construction
    sites -- this is the QA-real-app proof, not a mock of `dispatch`."""
    _seed(home, "T-1", phase=Phase.READY)
    claude_delegate = DryRunSessions()
    routing = RoutingSessions({"claude": claude_delegate})
    report = disp.dispatch(cfg, routing, now=1000)
    assert report.spawned == ["T-1"]
    assert [s[0] for s in claude_delegate.spawned] == ["T-1"]


# --- AC9: `runner` never becomes a human-only-adjacent agent verb -----------

def test_runner_is_not_an_agent_tool_verb():
    assert "runner" not in _AGENT_TOOL_VERBS
