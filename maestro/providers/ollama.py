"""Ollama HTTP adapter — discovers the local daemon's installed models and their
capabilities, for the ``maestro runners`` convenience listing and the
``check_ollama_models`` health check.

Talks to ``GET {OLLAMA_HOST}/api/tags`` over ``urllib.request`` only (stdlib, no
third-party dep — the core's constraint, same as ``jira.py``/``linear.py``), **not**
the ``ollama`` CLI: the CLI has no JSON output (its only flag is ``-h``) and,
decisively, carries no ``capabilities`` field, which is the single most valuable
thing this module answers (a model that can't call tools is useless for a
reconciler regardless of how good it otherwise is).

Never raises — like ``repos.resolve`` and ``health.check_gh_credential_reachability``,
a down or hung daemon must never wedge a caller (the dispatcher, a CLI verb, a TUI
modal). Every public entry point returns a value, never an exception, and always
passes an explicit short timeout: connection-refused is instant, but a *hung*
listener is the failure mode that actually freezes something.

Discovery here is a convenience only — the model field stays free text everywhere
else in maestro. Validation of one specific choice happens at spawn preflight
(OC-2), not here.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol

DEFAULT_HOST = "127.0.0.1:11434"
TIMEOUT_S = 2.0


class OllamaTransport(Protocol):
    """The external HTTP boundary — the only thing a test should fake."""

    def get(self, url: str) -> bytes: ...


class HttpOllamaTransport:
    """Real transport: a plain GET over urllib with an explicit short timeout."""

    def get(self, url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:
            return resp.read()


def base_url(host: str | None = None) -> str:
    """Resolve ``OLLAMA_HOST`` (env var, or the *host* override) to a full base
    URL. ``OLLAMA_HOST`` may or may not carry a scheme -- default to plain HTTP
    when one is absent, matching how ollama itself resolves the var."""
    resolved = host if host is not None else os.environ.get("OLLAMA_HOST", DEFAULT_HOST)
    if "://" not in resolved:
        resolved = f"http://{resolved}"
    return resolved.rstrip("/")


def fetch_models(
    host: str | None = None, *, transport: OllamaTransport | None = None
) -> tuple[list[dict] | None, str | None]:
    """List installed models with their capabilities via ``GET /api/tags``.
    Returns ``(models, None)`` on success or ``(None, reason)`` on any failure --
    connection refused, a hung listener past ``TIMEOUT_S``, or a malformed
    response all collapse to a reason string rather than an exception."""
    transport = transport or HttpOllamaTransport()
    url = f"{base_url(host)}/api/tags"
    try:
        raw = transport.get(url)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return None, f"{type(e).__name__}: {e}"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        return None, f"malformed response: {e}"
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None, "malformed response: no 'models' list"
    return models, None


def model_names(models: list[dict], *, tool_capable_only: bool = False) -> list[str]:
    """Installed model names, optionally filtered to those advertising the
    ``tools`` capability -- the filter that actually matters for driving an
    agent (an embedding-only or thinking-only model cannot)."""
    names = []
    for m in models:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        if tool_capable_only and "tools" not in (m.get("capabilities") or []):
            continue
        names.append(name)
    return names


def verdict_for_model(
    models: list[dict] | None, reason: str | None, model_name: str
) -> tuple[str, str | None]:
    """The three-valued verdict for one model name against a ``fetch_models()``
    result -- ``"ok"`` (installed and tool-capable), ``"missing"`` (daemon
    reachable but the model isn't installed, or is installed but can't call
    tools), or ``"unreachable"`` (the daemon itself couldn't be reached). That
    distinction is load-bearing for OC-2's transient-vs-permanent split:
    unreachable is worth retrying, missing needs a human (or ``ollama pull``)."""
    if models is None:
        return "unreachable", reason
    for m in models:
        name = m.get("name") or m.get("model")
        if name != model_name:
            continue
        if "tools" in (m.get("capabilities") or []):
            return "ok", None
        return "missing", f"{model_name} has no 'tools' capability (capabilities: {m.get('capabilities')})"
    return "missing", f"{model_name} not installed (run: ollama pull {model_name})"
