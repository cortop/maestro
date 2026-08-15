"""T-33 (RF-4): `health.check_ollama_models` -- board-wide visibility (never a
spawn gate; OC-2's spawn preflight is where a bad choice is actually refused)
into tickets whose spec names a non-claude `runner:` with a `runner_model:`
that's absent from, or not tool-capable on, the local ollama daemon."""
import io
import json
import sys

from maestro import cli, dispatcher as disp, event_log, health, snapshot as snap_mod, store
from maestro.providers import ollama as ollama_mod
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from test_health import _sweep

TOOL_CAPABLE = [
    {"name": "qwen3-coder:30b", "capabilities": ["completion", "tools"]},
]
NOT_TOOL_CAPABLE = [
    {"name": "mxbai-embed-large:latest", "capabilities": ["embedding"]},
]


class _FakeTransport:
    def __init__(self, body: bytes | None = None, raises: Exception | None = None):
        self.body = body
        self.raises = raises

    def get(self, url: str) -> bytes:
        if self.raises is not None:
            raise self.raises
        return self.body


def _tags_body(models):
    return json.dumps({"models": models}).encode()


def _seed_spec(home, key, text):
    store.atomic_write(store.spec_path(home, key), text)


def _claude_spec(key):
    return f"# {key}\napproval_tier: 1\n\n## Intent\n{key}\n"


def _runner_spec(key, runner, model):
    return f"# {key}\napproval_tier: 1\nrunner: {runner}\nrunner_model: {model}\n\n## Intent\n{key}\n"


# --- no network when nothing to check ------------------------------------------


def test_no_candidates_never_calls_the_transport(cfg, home):
    _seed_spec(home, "T-1", _claude_spec("T-1"))

    def boom(*a, **k):
        raise AssertionError("must not call the transport when no ticket uses a non-claude runner")
    transport = type("Boom", (), {"get": boom})()
    result = health.check_ollama_models(cfg, 1000, transport=transport)
    assert result["status"] == "ok"
    assert result["tickets"] == {}


def test_absent_runner_field_never_calls_the_transport(cfg, home):
    """A spec with no `runner:` line at all is claude by default -- never
    a candidate."""
    _seed_spec(home, "T-1", "# T-1\napproval_tier: 1\n\n## Intent\nx\n")

    def boom(*a, **k):
        raise AssertionError("must not call the transport")
    transport = type("Boom", (), {"get": boom})()
    result = health.check_ollama_models(cfg, 1000, transport=transport)
    assert result["status"] == "ok"


def test_runner_claude_explicit_never_calls_the_transport(cfg, home):
    _seed_spec(home, "T-1", _runner_spec("T-1", "claude", "sonnet"))

    def boom(*a, **k):
        raise AssertionError("must not call the transport for runner: claude")
    transport = type("Boom", (), {"get": boom})()
    result = health.check_ollama_models(cfg, 1000, transport=transport)
    assert result["status"] == "ok"


# --- ok / missing / unreachable -------------------------------------------------


def test_ok_when_model_installed_and_tool_capable(cfg, home):
    _seed_spec(home, "T-1", _runner_spec("T-1", "opencode", "qwen3-coder:30b"))
    transport = _FakeTransport(body=_tags_body(TOOL_CAPABLE))
    result = health.check_ollama_models(cfg, 1000, transport=transport)
    assert result["status"] == "ok"
    assert result["tickets"] == {}


def test_warn_when_model_absent(cfg, home):
    _seed_spec(home, "T-1", _runner_spec("T-1", "opencode", "llama99:1b"))
    transport = _FakeTransport(body=_tags_body(TOOL_CAPABLE))
    result = health.check_ollama_models(cfg, 1000, transport=transport)
    assert result["status"] == "warn"
    assert result["tickets"]["T-1"]["verdict"] == "missing"


def test_warn_when_model_not_tool_capable(cfg, home):
    _seed_spec(home, "T-1", _runner_spec("T-1", "opencode", "mxbai-embed-large:latest"))
    transport = _FakeTransport(body=_tags_body(NOT_TOOL_CAPABLE))
    result = health.check_ollama_models(cfg, 1000, transport=transport)
    assert result["status"] == "warn"
    assert result["tickets"]["T-1"]["verdict"] == "missing"


def test_warn_once_when_daemon_unreachable_with_multiple_candidates(cfg, home):
    _seed_spec(home, "T-1", _runner_spec("T-1", "opencode", "a:1b"))
    _seed_spec(home, "T-2", _runner_spec("T-2", "opencode", "b:1b"))
    transport = _FakeTransport(raises=ConnectionRefusedError("refused"))
    result = health.check_ollama_models(cfg, 1000, transport=transport)
    assert result["status"] == "warn"
    assert set(result["tickets"]) == {"T-1", "T-2"}
    assert all(t["verdict"] == "unreachable" for t in result["tickets"].values())


def test_ok_on_board_with_no_non_claude_tickets_even_if_daemon_unreachable(cfg, home):
    _seed_spec(home, "T-1", _claude_spec("T-1"))

    def boom(*a, **k):
        raise AssertionError("must not call the transport")
    transport = type("Boom", (), {"get": boom})()
    result = health.check_ollama_models(cfg, 1000, transport=transport)
    assert result["status"] == "ok"


# --- registered in CHECKS / real `maestro doctor --json` -----------------------


def test_check_name_appears_in_the_doctor_registry(cfg):
    names = {r["name"] for r in health.run_checks(cfg, 1000)}
    assert "ollama_models" in names


def test_real_doctor_json_warns_when_a_non_tool_capable_model_is_named(home, monkeypatch):
    _seed_spec(home, "T-1", _runner_spec("T-1", "opencode", "mxbai-embed-large:latest"))
    monkeypatch.setattr(ollama_mod, "HttpOllamaTransport",
                         lambda: _FakeTransport(body=_tags_body(NOT_TOOL_CAPABLE)))
    code, out = _sweep(home)
    assert code == 0  # WARN-only, never blocks a spawn
    check = next(c for c in out["checks"] if c["name"] == "ollama_models")
    assert check["status"] == "warn"
    assert check["tickets"]["T-1"]["verdict"] == "missing"


def test_real_doctor_json_ok_when_no_non_claude_ticket_exists(home, monkeypatch):
    _seed_spec(home, "T-1", _claude_spec("T-1"))

    def boom():
        raise AssertionError("must not build a transport when nothing needs checking")
    monkeypatch.setattr(ollama_mod, "HttpOllamaTransport", boom)
    code, out = _sweep(home)
    assert code == 0
    check = next(c for c in out["checks"] if c["name"] == "ollama_models")
    assert check["status"] == "ok"


# --- AC6: never called from the hot paths (mint / `maestro env --key` / a sweep) --


def _boom_transport(monkeypatch):
    def boom():
        raise AssertionError("must not build an ollama transport on this path")
    monkeypatch.setattr(ollama_mod, "HttpOllamaTransport", boom)


def test_maestro_env_key_never_touches_ollama(home, monkeypatch):
    _seed_spec(home, "T-1", _runner_spec("T-1", "opencode", "a:1b"))
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1", "spec_hash": "x"}, actor="d")
    snap_mod.rebuild(home, "T-1")
    _boom_transport(monkeypatch)

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(home), "env", "--key", "T-1"])
    finally:
        sys.stdout = old
    assert code == 0


def test_real_sweep_with_no_non_claude_ticket_never_touches_ollama(home, cfg, monkeypatch):
    store.atomic_write(store.spec_path(home, "T-1"), _claude_spec("T-1"))
    event_log.append(home, "T-1", "TicketCreated",
                      {"title": "T-1", "spec_hash": disp.spec_hash_on_disk(home, "T-1")}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.READY.value}, actor="r")
    snap_mod.rebuild(home, "T-1")
    _boom_transport(monkeypatch)

    report = disp.dispatch(cfg, DryRunSessions(), 1000)
    assert report is not None


def test_mint_new_tickets_never_touches_ollama(home, cfg, monkeypatch):
    _boom_transport(monkeypatch)
    disp.mint_new_tickets(cfg)  # no `_new` inbox entries -- proves the path is reachable and untouched


# --- T-61 (PI-9): a pi ticket is never an ollama_models candidate ------------


def test_pi_runner_never_calls_the_transport(cfg, home):
    """A `runner: pi` ticket is NOT an ollama_models candidate -- ollama is
    opencode's own backend, not a catalogue every non-claude runner shares."""
    _seed_spec(home, "T-1", _runner_spec("T-1", "pi", "glm-5.2"))

    def boom(*a, **k):
        raise AssertionError("must not call the transport for runner: pi")
    transport = type("Boom", (), {"get": boom})()
    result = health.check_ollama_models(cfg, 1000, transport=transport)
    assert result["status"] == "ok"
    assert result["tickets"] == {}


def test_real_doctor_json_ok_for_pi_only_board_makes_zero_ollama_requests(home, monkeypatch):
    """AC3: a real `maestro doctor` over a board whose only non-claude ticket
    names `runner: pi` returns `ok` for `ollama_models` and makes ZERO ollama
    requests."""
    _seed_spec(home, "T-1", _runner_spec("T-1", "pi", "glm-5.2"))
    _boom_transport(monkeypatch)
    code, out = _sweep(home)
    assert code == 0
    check = next(c for c in out["checks"] if c["name"] == "ollama_models")
    assert check["status"] == "ok"


def test_real_doctor_json_warns_once_when_daemon_unreachable(home, monkeypatch):
    _seed_spec(home, "T-1", _runner_spec("T-1", "opencode", "a:1b"))
    monkeypatch.setattr(ollama_mod, "HttpOllamaTransport",
                         lambda: _FakeTransport(raises=ConnectionRefusedError("refused")))
    code, out = _sweep(home)
    assert code == 0
    check = next(c for c in out["checks"] if c["name"] == "ollama_models")
    assert check["status"] == "warn"
    assert check["tickets"]["T-1"]["verdict"] == "unreachable"
