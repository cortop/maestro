"""T-33 (RF-4): `maestro runners` -- the first visible deliverable of ollama
discovery. Real `cli.main([..., "runners"])` over a temp home, transport
injected by swapping `ollama.HttpOllamaTransport` (same technique as
`check_ollama_models`'s tests) -- never a real network call in a test."""
import io
import json
import sys

from maestro import cli
from maestro.providers import ollama as ollama_mod

FIVE_MODELS = [
    {"name": "qwen3-coder:30b", "capabilities": ["completion", "tools"]},
    {"name": "deepseek-r1:70b-llama-distill-q8_0", "capabilities": ["completion", "thinking"]},
    {"name": "mxbai-embed-large:latest", "capabilities": ["embedding"]},
    {"name": "qwen3:8b", "capabilities": ["completion", "tools", "thinking"]},
]


class _FakeTransport:
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
    return json.dumps({"models": models}).encode()


def _run_cli(home, *extra_args):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(home), "runners", *extra_args])
    finally:
        sys.stdout = old
    return code, json.loads(buf.getvalue())


def test_runners_lists_claude_plus_tool_capable_opencode_models(home, monkeypatch):
    monkeypatch.setattr(ollama_mod, "HttpOllamaTransport",
                         lambda: _FakeTransport(body=_tags_body(FIVE_MODELS)))
    code, out = _run_cli(home)
    assert code == 0
    assert out["claude"]["status"] == "ok"
    assert out["opencode"]["status"] == "ok"
    assert set(out["opencode"]["models"]) == {"qwen3-coder:30b", "qwen3:8b"}
    assert "mxbai-embed-large:latest" not in out["opencode"]["models"]  # embedding-only
    assert "deepseek-r1:70b-llama-distill-q8_0" not in out["opencode"]["models"]  # thinking-only


def test_runners_connection_refused_exits_0_with_null_models_and_reason(home, monkeypatch):
    monkeypatch.setattr(ollama_mod, "HttpOllamaTransport",
                         lambda: _FakeTransport(raises=ConnectionRefusedError("refused")))
    code, out = _run_cli(home)
    assert code == 0
    assert out["claude"]["status"] == "ok"
    assert out["opencode"]["status"] == "unreachable"
    assert out["opencode"]["models"] is None
    assert "refused" in out["opencode"]["reason"] or "ConnectionRefusedError" in out["opencode"]["reason"]


def test_runners_timeout_exits_0_with_null_models_and_reason(home, monkeypatch):
    monkeypatch.setattr(ollama_mod, "HttpOllamaTransport",
                         lambda: _FakeTransport(raises=TimeoutError("timed out")))
    code, out = _run_cli(home)
    assert code == 0
    assert out["opencode"]["models"] is None
    assert out["opencode"]["reason"] is not None


def test_runners_malformed_json_exits_0_with_null_models_and_reason(home, monkeypatch):
    monkeypatch.setattr(ollama_mod, "HttpOllamaTransport",
                         lambda: _FakeTransport(body=b"not json{{{"))
    code, out = _run_cli(home)
    assert code == 0
    assert out["opencode"]["models"] is None
    assert "malformed" in out["opencode"]["reason"]


def test_runners_passes_the_resolved_url_regardless_of_ollama_host_scheme(home, monkeypatch):
    fake = _FakeTransport(body=_tags_body([]))
    monkeypatch.setattr(ollama_mod, "HttpOllamaTransport", lambda: fake)
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    _run_cli(home)
    assert fake.urls == ["http://127.0.0.1:11434/api/tags"]
