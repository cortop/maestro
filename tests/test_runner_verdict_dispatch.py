"""PI-3: the permanent model-verdict branch resolves its verdict function per
runner instead of importing `providers.ollama` unconditionally. Mirrors
`_make_default_runner_probe`'s `{runner name: fn}` dispatch table -- given a
runner name, `_make_default_runner_verdict(cfg)` (the default resolver) or an
injected `runner_verdict` (test override, same one-arg shape as `runner_probe`)
returns the `(models, daemon_reason, runner_model) -> (verdict, reason)`
function to judge that runner's catalogue with. This file proves the
resolution is real: an injected runner-specific verdict function's own reason
text -- not a generic ollama-derived one -- surfaces in the `ops.ask` question,
and the resolver is actually called with the runner name under test.
"""
from __future__ import annotations

from maestro import dispatcher as disp, event_log, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

RUNNER = "opencode"


def _register(monkeypatch, *names):
    monkeypatch.setattr(disp, "_REGISTERED_RUNNERS", frozenset({"claude", *names}))


def _enable(cfg, *names):
    cfg.runner_enabled = ["claude", *names]


def _seed(home, key, *, runner, runner_model, approval_tier=0):
    spec = (f"# {key}\napproval_tier: {approval_tier}\n"
            f"runner: {runner}\nrunner_model: {runner_model}\ndependsOn: []\n\n"
            "## Acceptance criteria\n- [ ] ok\n")
    store.atomic_write(store.spec_path(home, key), spec)
    event_log.append(home, key, "TicketCreated",
                      {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, key)


def _probe(models):
    return lambda runner: {"binary_ok": True, "models": models, "daemon_reason": None}


def test_injected_runner_verdict_reason_surfaces_in_ops_ask(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _enable(cfg, RUNNER)
    _seed(home, "BAD-1", runner=RUNNER, runner_model="widget:1b")

    calls = []

    def fake_verdict(models, daemon_reason, runner_model):
        return "missing", "widget-flavored reason unique to this runner"

    def runner_verdict(runner):
        calls.append(runner)
        return fake_verdict

    report = disp.dispatch(cfg, DryRunSessions(), now=1000,
                            runner_probe=_probe([]), runner_verdict=runner_verdict)

    assert "BAD-1" not in report.spawned
    assert calls == [RUNNER]  # the resolver was actually called with the runner name

    events = event_log.read(home, "BAD-1")
    asked = [e for e in events if e["type"] == "QuestionAsked"]
    assert len(asked) == 1
    assert "widget-flavored reason unique to this runner" in asked[0]["payload"]["text"]

    snap = snap_mod.load(home, "BAD-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value


def test_default_resolver_dispatches_per_runner_name(home, cfg, monkeypatch):
    # No injected `runner_verdict` -- exercises the real default resolver,
    # `_make_default_runner_verdict(cfg)`, for a non-"ollama" runner name.
    # Per the spec's fallback Note, an unrecognized runner still gets the
    # ollama-backed verdict function, so a tool-capable model on the injected
    # probe's catalogue verdicts "ok" and the key spawns -- proving the
    # resolver is reached (not skipped/short-circuited) and returns a callable
    # verdict function, not a crash from an unconditional bad import.
    _register(monkeypatch, RUNNER)
    _enable(cfg, RUNNER)
    _seed(home, "OK-1", runner=RUNNER, runner_model="widget:1b")
    probe = _probe([{"name": "widget:1b", "capabilities": ["tools", "completion"]}])

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)

    assert "OK-1" in report.spawned
