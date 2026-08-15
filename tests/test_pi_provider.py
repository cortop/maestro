"""T-57 (PI-5): `providers/pi.py` -- the pi CLI adapter and its three-valued
`verdict_for_model`, publishing the identical contract `providers/ollama.py`
does (`fetch_models`/`model_names`/`verdict_for_model`) so PI-3's generalized
per-runner probe table (`dispatcher._make_default_runner_probe`/
`_make_default_runner_verdict`) works with no special-casing.

The only mock is the subprocess boundary itself (`run`, `providers.pi`'s own
injectable, matching `health.check_pi_version`'s established pattern for a pi
subprocess call in this codebase) -- fixture files under `tests/fixtures/`
hold REAL captured `pi --list-models` stdout from the installed 0.79.2, never
hand-written.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from maestro import dispatcher as disp, event_log, snapshot as snap_mod, store
from maestro.providers import pi as pi_mod
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

FIXTURES = Path(__file__).parent / "fixtures"
NO_CREDENTIALS_STDOUT = (FIXTURES / "pi_list_models_no_credentials.txt").read_text(encoding="utf-8")
TABLE_STDOUT = (FIXTURES / "pi_list_models_table.txt").read_text(encoding="utf-8")


def _fake_run(stdout="", returncode=0, *, raise_exc=None):
    calls = []

    def run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        if raise_exc is not None:
            raise raise_exc
        p = subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")
        return p

    run.calls = calls
    return run


# --- AC1: the three-function contract, exact verdict strings ----------------------

def test_module_exposes_the_three_functions():
    assert callable(pi_mod.fetch_models)
    assert callable(pi_mod.model_names)
    assert callable(pi_mod.verdict_for_model)


def test_verdict_strings_are_exactly_ok_missing_unreachable():
    models = [{"provider": "zai", "model": "glm-5.2", "context": "1.0M",
               "max-out": "128K", "thinking": "yes", "images": "no"}]
    assert pi_mod.verdict_for_model(models, None, "glm-5.2") == ("ok", None)
    verdict, reason = pi_mod.verdict_for_model(models, None, "nonexistent-model")
    assert verdict == "missing" and reason
    verdict, reason = pi_mod.verdict_for_model(None, "boom", "glm-5.2")
    assert verdict == "unreachable" and reason == "boom"


# --- AC2: fetch_models never raises -- injected `run` for all 4 failure modes -----

def test_fetch_models_missing_binary_never_raises():
    run = _fake_run(raise_exc=FileNotFoundError("no such file: pi"))
    models, reason = pi_mod.fetch_models(run=run)
    assert models is None
    assert "pi" in reason.lower() or "no such file" in reason.lower()


def test_fetch_models_non_zero_exit_never_raises():
    run = _fake_run(stdout="", returncode=1)
    models, reason = pi_mod.fetch_models(run=run)
    assert models is None
    assert "1" in reason


def test_fetch_models_timeout_never_raises():
    run = _fake_run(raise_exc=subprocess.TimeoutExpired(cmd=["pi"], timeout=15.0))
    models, reason = pi_mod.fetch_models(run=run)
    assert models is None
    assert "timed out" in reason


def test_fetch_models_unparseable_stdout_never_raises():
    run = _fake_run(stdout="this is not a table at all\njust garbage\n", returncode=0)
    models, reason = pi_mod.fetch_models(run=run)
    assert models is None
    assert "unparseable" in reason.lower()


# --- AC3: exit code is ignored -- captured real "no credentials" stdout, exit 0 ---

def test_no_credentials_real_stdout_exit_0_yields_unreachable_not_ok():
    run = _fake_run(stdout=NO_CREDENTIALS_STDOUT, returncode=0)
    models, reason = pi_mod.fetch_models(run=run)
    assert models is None
    verdict, _ = pi_mod.verdict_for_model(models, reason, "glm-5.2")
    assert verdict == "unreachable"


# --- AC4: zero models -> unreachable; id absent from a nonempty list -> missing ---
# Both against captured real table output.

def test_zero_models_classifies_as_unreachable_against_real_output():
    run = _fake_run(stdout=NO_CREDENTIALS_STDOUT, returncode=0)
    models, reason = pi_mod.fetch_models(run=run)
    verdict, _ = pi_mod.verdict_for_model(models, reason, "anything")
    assert verdict == "unreachable"


def test_id_absent_from_nonempty_table_classifies_as_missing_against_real_output():
    run = _fake_run(stdout=TABLE_STDOUT, returncode=0)
    models, reason = pi_mod.fetch_models(run=run)
    assert models  # a real, nonempty, successfully parsed table
    verdict, detail = pi_mod.verdict_for_model(models, reason, "glm-9.9-does-not-exist")
    assert verdict == "missing"
    assert "glm-9.9-does-not-exist" in detail


def test_id_present_in_real_table_classifies_as_ok():
    run = _fake_run(stdout=TABLE_STDOUT, returncode=0)
    models, reason = pi_mod.fetch_models(run=run)
    verdict, detail = pi_mod.verdict_for_model(models, reason, "glm-5.2")
    assert (verdict, detail) == ("ok", None)


# --- AC5: model_names returns bare ids, no provider/ prefix -----------------------

def test_model_names_are_bare_ids_no_provider_prefix():
    run = _fake_run(stdout=TABLE_STDOUT, returncode=0)
    models, _ = pi_mod.fetch_models(run=run)
    names = pi_mod.model_names(models)
    assert "glm-5.2" in names
    assert all("/" not in n for n in names)
    assert all(not n.startswith("zai") for n in names)


# --- AC6: subprocess runs under PI_CODING_AGENT_DIR with the offline flag ---------

def test_fetch_models_sets_pi_coding_agent_dir_env_and_offline_flag(tmp_path):
    agent_dir = tmp_path / ".pi-agent"
    run = _fake_run(stdout=TABLE_STDOUT, returncode=0)
    pi_mod.fetch_models(agent_dir, run=run)
    assert len(run.calls) == 1
    call = run.calls[0]
    assert "--offline" in call["argv"]
    assert call["env"]["PI_CODING_AGENT_DIR"] == str(agent_dir)


def test_default_dispatcher_probe_closure_threads_pi_agent_dir_and_offline(cfg, monkeypatch):
    # PI-3 makes the default probe a closure over cfg precisely so this is
    # reachable -- proves the REAL production wiring (not a test-injected
    # runner_probe) resolves PI-4's `store.pi_agent_dir(cfg.home)` and passes
    # the offline flag, not just that `fetch_models` can do it in isolation.
    run = _fake_run(stdout=TABLE_STDOUT, returncode=0)
    monkeypatch.setattr(pi_mod.subprocess, "run", run)
    probe = disp._make_default_runner_probe(cfg)
    probed = probe("pi")
    assert probed["models"]
    assert len(run.calls) == 1
    call = run.calls[0]
    assert "--offline" in call["argv"]
    assert call["env"]["PI_CODING_AGENT_DIR"] == str(store.pi_agent_dir(cfg.home))


def test_default_dispatcher_verdict_closure_resolves_pi_verdict_for_model(cfg):
    resolver = disp._make_default_runner_verdict(cfg)
    assert resolver("pi") is pi_mod.verdict_for_model


# --- AC7/AC8: a real dispatch sweep, missing (ask once, no dup) vs unreachable ----

def _register(monkeypatch, *names):
    monkeypatch.setattr(disp, "_REGISTERED_RUNNERS", frozenset({"claude", *names}))


def _enable(cfg, *names):
    cfg.runner_enabled = ["claude", *names]


def _seed(home, key, *, runner, runner_model):
    spec = (f"# {key}\napproval_tier: 0\n"
            f"runner: {runner}\nrunner_model: {runner_model}\ndependsOn: []\n")
    store.atomic_write(store.spec_path(home, key), spec)
    event_log.append(home, key, "TicketCreated",
                      {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, key)


def _missing_probe():
    # A real, nonempty, parsed table -- pi is reachable but the configured
    # runner_model isn't in it.
    models, _ = pi_mod.fetch_models(run=_fake_run(stdout=TABLE_STDOUT, returncode=0))
    return lambda runner: {"binary_ok": True, "models": models, "daemon_reason": None}


def _unreachable_probe():
    # A real "no credentials" fetch -- fetch_models' own zero-models shape.
    models, reason = pi_mod.fetch_models(run=_fake_run(stdout=NO_CREDENTIALS_STDOUT, returncode=0))
    assert models is None
    return lambda runner: {"binary_ok": True, "models": models, "daemon_reason": reason}


def test_real_sweep_missing_verdict_asks_once_no_duplicate_no_ledger_spend(home, cfg, monkeypatch):
    _register(monkeypatch, "pi")
    _enable(cfg, "pi")
    _seed(home, "T-1", runner="pi", runner_model="glm-9.9-does-not-exist")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=_missing_probe())
    assert "T-1" not in report.spawned

    events = event_log.read(home, "T-1")
    asked = [e for e in events if e["type"] == "QuestionAsked"]
    assert len(asked) == 1
    qid = asked[0]["payload"]["qid"]

    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert snap.failure_count == 0

    attempts = store.read_json(disp._spawn_attempts_path(home), {})
    assert "T-1" not in attempts

    # Second sweep: the ticket is now awaiting-human, not due -- but even a
    # direct re-ask with the same qid must stay idempotent (no duplicate).
    disp.dispatch(cfg, DryRunSessions(), now=2000, runner_probe=_missing_probe())
    events = event_log.read(home, "T-1")
    asked = [e for e in events if e["type"] == "QuestionAsked" and e["payload"]["qid"] == qid]
    assert len(asked) == 1


def test_real_sweep_unreachable_verdict_spends_no_ledger_slot_and_spawns_nothing(home, cfg, monkeypatch):
    _register(monkeypatch, "pi")
    _enable(cfg, "pi")
    _seed(home, "T-1", runner="pi", runner_model="glm-5.2")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=_unreachable_probe())
    assert "T-1" not in report.spawned

    events = event_log.read(home, "T-1")
    assert not [e for e in events if e["type"] == "QuestionAsked"]

    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.IMPLEMENTING.value  # never routed to awaiting-human

    attempts = store.read_json(disp._spawn_attempts_path(home), {})
    assert "T-1" not in attempts
