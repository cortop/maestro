"""T-61 (PI-9): `health.check_pi_models` -- pi's own counterpart of
`check_ollama_models`, board-wide visibility (never a spawn gate; OC-2's spawn
preflight is where a bad choice is actually refused) into tickets whose spec
names `runner: pi` with a `runner_model:` absent from pi's own catalogue."""
import io
import json
import subprocess
import sys

from maestro import cli, health, store

TABLE_STDOUT = (
    "provider  model    context  max-out  thinking  images\n"
    "zai       glm-5.2  1.0M     128K     yes       no\n"
)
NO_MODELS_STDOUT = "No models available -- configure a provider first.\n"


def _fake_run(stdout="", returncode=0, *, raise_exc=None):
    def run(argv, **kwargs):
        if raise_exc is not None:
            raise raise_exc
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")
    return run


def _seed_spec(home, key, text):
    store.atomic_write(store.spec_path(home, key), text)


def _claude_spec(key):
    return f"# {key}\napproval_tier: 1\n\n## Intent\n{key}\n"


def _runner_spec(key, runner, model):
    return f"# {key}\napproval_tier: 1\nrunner: {runner}\nrunner_model: {model}\n\n## Intent\n{key}\n"


def _sweep(home):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(home), "doctor"])
    finally:
        sys.stdout = old
    return code, json.loads(buf.getvalue())


# --- no subprocess call when nothing to check -----------------------------------


def test_no_candidates_never_shells_pi(cfg, home):
    _seed_spec(home, "T-1", _claude_spec("T-1"))

    def boom(*a, **k):
        raise AssertionError("must not shell pi when no ticket names runner: pi")
    result = health.check_pi_models(cfg, 1000, run=boom)
    assert result["status"] == "ok"
    assert result["tickets"] == {}


def test_opencode_runner_never_shells_pi(cfg, home):
    """An opencode ticket is `check_ollama_models`'s own candidate, never
    this check's."""
    _seed_spec(home, "T-1", _runner_spec("T-1", "opencode", "qwen3-coder:30b"))

    def boom(*a, **k):
        raise AssertionError("must not shell pi for runner: opencode")
    result = health.check_pi_models(cfg, 1000, run=boom)
    assert result["status"] == "ok"


# --- ok / missing / unreachable --------------------------------------------------


def test_ok_when_model_present_in_pis_catalogue(cfg, home):
    _seed_spec(home, "T-1", _runner_spec("T-1", "pi", "glm-5.2"))
    result = health.check_pi_models(cfg, 1000, run=_fake_run(stdout=TABLE_STDOUT))
    assert result["status"] == "ok"
    assert result["tickets"] == {}


def test_warn_when_model_absent_from_pis_catalogue(cfg, home):
    _seed_spec(home, "T-1", _runner_spec("T-1", "pi", "glm-9.9-does-not-exist"))
    result = health.check_pi_models(cfg, 1000, run=_fake_run(stdout=TABLE_STDOUT))
    assert result["status"] == "warn"
    assert result["tickets"]["T-1"]["verdict"] == "missing"


def test_warn_once_when_pi_unreachable_with_multiple_candidates(cfg, home):
    _seed_spec(home, "T-1", _runner_spec("T-1", "pi", "a"))
    _seed_spec(home, "T-2", _runner_spec("T-2", "pi", "b"))
    result = health.check_pi_models(cfg, 1000, run=_fake_run(stdout=NO_MODELS_STDOUT))
    assert result["status"] == "warn"
    assert set(result["tickets"]) == {"T-1", "T-2"}
    assert all(t["verdict"] == "unreachable" for t in result["tickets"].values())


def test_missing_binary_never_raises_and_reports_unreachable(cfg, home):
    _seed_spec(home, "T-1", _runner_spec("T-1", "pi", "glm-5.2"))
    result = health.check_pi_models(cfg, 1000, run=_fake_run(raise_exc=FileNotFoundError("no pi")))
    assert result["status"] == "warn"
    assert result["tickets"]["T-1"]["verdict"] == "unreachable"


# --- registered in CHECKS / real `maestro doctor --json` -----------------------


def test_check_name_appears_in_the_doctor_registry(cfg):
    names = {r["name"] for r in health.run_checks(cfg, 1000)}
    assert "pi_models" in names


def test_real_doctor_json_warns_when_pi_model_is_absent(home, monkeypatch):
    _seed_spec(home, "T-1", _runner_spec("T-1", "pi", "glm-9.9-does-not-exist"))
    monkeypatch.setattr("maestro.providers.pi.subprocess.run", _fake_run(stdout=TABLE_STDOUT))
    code, out = _sweep(home)
    assert code == 0  # WARN-only, never blocks a spawn
    check = next(c for c in out["checks"] if c["name"] == "pi_models")
    assert check["status"] == "warn"
    assert check["tickets"]["T-1"]["verdict"] == "missing"


def test_real_doctor_json_ok_when_no_pi_ticket_exists(home, monkeypatch):
    _seed_spec(home, "T-1", _claude_spec("T-1"))

    def boom(*a, **k):
        raise AssertionError("must not shell pi when nothing needs checking")
    monkeypatch.setattr("maestro.providers.pi.subprocess.run", boom)
    code, out = _sweep(home)
    assert code == 0
    check = next(c for c in out["checks"] if c["name"] == "pi_models")
    assert check["status"] == "ok"
