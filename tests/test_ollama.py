"""T-33 (RF-4): `providers/ollama.py`'s HTTP discovery -- `GET /api/tags` over an
injectable transport, never raises, three-valued `ok`/`missing`/`unreachable`
verdicts. The only mock is the transport itself (the external HTTP boundary)."""
import pytest

from maestro.providers import ollama as ollama_mod

FIVE_MODELS = [
    {"name": "qwen3-coder:30b", "capabilities": ["completion", "tools"]},
    {"name": "deepseek-r1:70b-llama-distill-q8_0", "capabilities": ["completion", "thinking"]},
    {"name": "mxbai-embed-large:latest", "capabilities": ["embedding"]},
    {"name": "qwen3:8b", "capabilities": ["completion", "tools", "thinking"]},
    {"name": "qwen3.5:latest", "capabilities": ["vision", "completion", "tools", "thinking"]},
]


class _FakeTransport:
    """Records the URL it was asked to GET; returns a canned body or raises a
    canned exception -- the only thing a test of this module should fake."""

    def __init__(self, body: bytes | None = None, raises: Exception | None = None):
        self.body = body
        self.raises = raises
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if self.raises is not None:
            raise self.raises
        return self.body


def _tags_body(models):
    import json
    return json.dumps({"models": models}).encode()


# --- fetch_models -------------------------------------------------------------


def test_fetch_models_returns_full_list_on_success():
    transport = _FakeTransport(body=_tags_body(FIVE_MODELS))
    models, reason = ollama_mod.fetch_models(transport=transport)
    assert reason is None
    assert [m["name"] for m in models] == [m["name"] for m in FIVE_MODELS]


@pytest.mark.parametrize("exc", [
    ConnectionRefusedError("refused"),
    TimeoutError("timed out"),
    OSError("boom"),
])
def test_fetch_models_never_raises_on_transport_failure(exc):
    transport = _FakeTransport(raises=exc)
    models, reason = ollama_mod.fetch_models(transport=transport)
    assert models is None
    assert reason is not None
    assert type(exc).__name__ in reason


def test_fetch_models_never_raises_on_malformed_json():
    transport = _FakeTransport(body=b"not json{{{")
    models, reason = ollama_mod.fetch_models(transport=transport)
    assert models is None
    assert "malformed" in reason


def test_fetch_models_never_raises_when_models_key_missing():
    transport = _FakeTransport(body=b"{}")
    models, reason = ollama_mod.fetch_models(transport=transport)
    assert models is None
    assert "malformed" in reason


# --- base_url / OLLAMA_HOST resolution -----------------------------------------


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("https://ollama.example.com", "https://ollama.example.com"),
    ("ollama.example.com:11434/", "http://ollama.example.com:11434"),
])
def test_base_url_resolves_with_and_without_a_scheme(host, expected):
    assert ollama_mod.base_url(host) == expected


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
])
def test_fetch_models_passes_the_resolved_url_to_the_transport(host, expected):
    transport = _FakeTransport(body=_tags_body([]))
    ollama_mod.fetch_models(host, transport=transport)
    assert transport.urls == [f"{expected}/api/tags"]


def test_base_url_falls_back_to_ollama_host_env_var(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "10.0.0.5:9999")
    assert ollama_mod.base_url() == "http://10.0.0.5:9999"


def test_base_url_defaults_when_no_env_var_set(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert ollama_mod.base_url() == f"http://{ollama_mod.DEFAULT_HOST}"


# --- model_names ----------------------------------------------------------------


def test_model_names_tool_capable_only_excludes_embedding_and_thinking_only():
    names = ollama_mod.model_names(FIVE_MODELS, tool_capable_only=True)
    assert names == ["qwen3-coder:30b", "qwen3:8b", "qwen3.5:latest"]
    assert "mxbai-embed-large:latest" not in names
    assert "deepseek-r1:70b-llama-distill-q8_0" not in names


def test_model_names_untfiltered_includes_everything():
    names = ollama_mod.model_names(FIVE_MODELS)
    assert len(names) == 5


# --- verdict_for_model: three-valued ok/missing/unreachable ---------------------


def test_verdict_for_model_ok_when_installed_and_tool_capable():
    verdict, reason = ollama_mod.verdict_for_model(FIVE_MODELS, None, "qwen3-coder:30b")
    assert (verdict, reason) == ("ok", None)


def test_verdict_for_model_missing_when_not_tool_capable():
    verdict, reason = ollama_mod.verdict_for_model(FIVE_MODELS, None, "mxbai-embed-large:latest")
    assert verdict == "missing"
    assert "tools" in reason


def test_verdict_for_model_missing_when_absent_gives_actionable_pull_hint():
    verdict, reason = ollama_mod.verdict_for_model(FIVE_MODELS, None, "llama99:1b")
    assert verdict == "missing"
    assert "ollama pull llama99:1b" in reason


def test_verdict_for_model_unreachable_when_models_is_none():
    verdict, reason = ollama_mod.verdict_for_model(None, "ConnectionRefusedError: refused", "qwen3:8b")
    assert verdict == "unreachable"
    assert reason == "ConnectionRefusedError: refused"
