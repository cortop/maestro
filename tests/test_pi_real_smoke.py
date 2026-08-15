"""PI-8 AC12: the ONE test in this suite that shells a REAL, non-stub `pi` --
proving the silent-no-op trust trap (module docstring, `sessions.PiCliSessions`)
is genuinely avoided, not accidentally dodged by a stub that never exercises
pi's own discovery/trust machinery at all.

Runs `PiCliSessions.spawn` for real, in an UNTRUSTED cwd (a fresh tmp dir with
no `.pi/` trust marker) with `--approve` never present, and asserts the
captured `.pi.jsonl` transcript's first `message_start` record shows
`/maestro-reconcile-implementing T-<key>` fully expanded from
`--prompt-template <payload dir>` with `$1` substituted to the real key --
never sent to the model as literal, unexpanded text (the failure mode this
whole backend exists to make impossible).

Needs a REAL, reachable model to get past pi's own credential check before it
will emit a single record (verified live against the installed 0.79.2: a
provider with no resolvable credentials fails before `agent_start`, so a
dummy/zai config can't reach the assertion this AC cares about) -- rather than
depend on a cloud vendor's credentials being configured wherever this suite
runs, this test points pi at a LOCAL ollama daemon (`providers.ollama`, this
codebase's own existing no-cost/no-network-egress dependency, already assumed
reachable by `test_ollama_health.py`'s own real-daemon tests) via a hand-built
`[runner.pi]`-shaped provider entry -- the exact same mechanism a real board's
own `[runner.pi]` config uses for any custom provider, isolated in a tmp
`PI_CODING_AGENT_DIR` (PI-4) the whole way, never the machine's real ~/.pi.
Skipped, with a clear reason, when `pi` isn't on PATH or no local ollama model
is reachable.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from maestro import claims, store
from maestro.providers import ollama as ollama_mod
from maestro.sessions import PiCliSessions

_PI_MISSING = shutil.which("pi") is None
_OLLAMA_MODELS, _OLLAMA_REASON = (None, "skipped: pi missing") if _PI_MISSING else ollama_mod.fetch_models()

pytestmark = pytest.mark.skipif(
    _PI_MISSING or not _OLLAMA_MODELS,
    reason=("real `pi` binary not on PATH" if _PI_MISSING else
            f"no local ollama model reachable to authenticate pi's real spawn against: {_OLLAMA_REASON}"))


def test_real_pi_expands_prompt_template_with_key_substituted_in_untrusted_cwd(
        home, tmp_path):
    model_tag = _OLLAMA_MODELS[0]["name"]

    untrusted_cwd = tmp_path / "untrusted-project"
    untrusted_cwd.mkdir()

    # PI-4: isolate from a developer's real ~/.pi -- same env_overlay shape
    # RoutingSessions._prep_pi_env prepares for a production spawn. Register a
    # "local-ollama" provider pointing at the real local daemon's
    # openai-compatible endpoint -- the exact mechanism a real board's own
    # `[runner.pi]` config uses for any custom provider, never the machine's
    # ambient ~/.pi settings.
    pi_dir = store.pi_agent_dir(home)
    pi_config = {
        "provider": "local-ollama",
        "base_url": f"{ollama_mod.base_url()}/v1",
        "api": "openai-completions",
        "api_key": "not-needed",
        "models": {model_tag: {"context_window": 32768, "max_tokens": 4096}},
    }
    store.generate_pi_models_json(home, pi_config)
    store.atomic_write(home / "config.toml",
        "[maestro]\nrepo_path = \"/repo/default\"\nbranch_prefix = \"maestro/\"\n"
        "min_spawn_interval = 0\n\n[runner.pi]\nprovider = \"local-ollama\"\n")

    env_overlay = {"PI_CODING_AGENT_DIR": str(pi_dir)}

    sess = PiCliSessions(home=home, capture_session_logs=True)
    key = "T-999"
    pid = sess.spawn(key, "/maestro-reconcile-implementing", cwd=untrusted_cwd,
                     runner_model=model_tag, env_overlay=env_overlay)
    assert pid is not None
    try:
        log_path = Path(claims.read_claim(home, key)["log_path"])

        transcript = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            if log_path.exists():
                transcript = log_path.read_text(encoding="utf-8", errors="ignore")
                if "message_start" in transcript:
                    break
            time.sleep(0.5)

        assert transcript, f"pi never wrote a transcript to {log_path} within 30s"
        assert "message_start" in transcript, \
            (f"pi never emitted a message_start record -- template expansion "
             f"never happened. Transcript so far:\n{transcript[:2000]}")
        # The exact heading `/maestro-reconcile-implementing` expands to (its
        # own `$1` slot substituted with the real key) -- proves this was
        # loaded from --prompt-template and expanded, not sent as literal
        # unexpanded text (the silent-no-op trap this backend exists to
        # avoid).
        assert f"reconcile `{key}`" in transcript, (
            "the expanded prompt template's own heading (with $1 substituted) "
            f"was not found in the transcript -- got:\n{transcript[:2000]}")
        # Never sent as the literal, unexpanded slash command -- the actual
        # failure mode this AC exists to rule out.
        assert f"/maestro-reconcile-implementing {key}" not in transcript
    finally:
        try:
            os.killpg(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
