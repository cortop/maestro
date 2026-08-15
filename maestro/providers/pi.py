"""pi CLI adapter -- discovers the models a configured ``pi`` install (the
``@earendil-works/pi-coding-agent`` binary) can actually resolve, and judges
one ``runner_model`` against that catalogue.

**pi cannot be trusted to validate a model itself.** Verified on the installed
0.79.2: ``pi --list-models`` exits **0** whether or not any model resolved (a
credential-less install prints "No models available..." to stdout, still
exit 0), and an unknown model under a *custom* provider is only a warning
followed by a live API call (see ``pi -p ... --model <provider>/<bogus>``).
So maestro owns the verdict here, the same way ``providers/ollama.py`` owns it
for a local daemon -- this module publishes the identical three-function
contract (``fetch_models``/``model_names``/``verdict_for_model``) so PI-3's
generalized, per-runner probe table (``dispatcher._make_default_runner_probe``
/``_make_default_runner_verdict``) works with no special-casing.

Shells out to ``pi --list-models --offline`` (never the HTTP boundary ollama
uses -- pi has no daemon) and parses its fixed-width stdout table. Like
``providers.ollama.fetch_models``, this function must NEVER raise: a missing
binary, non-zero exit, timeout, or unparseable stdout each collapse to
``(None, reason)`` rather than an exception reaching a caller (the dispatcher,
a CLI verb) that must never wedge on a broken pi install.

The distinction that matters for OC-2's transient-vs-permanent split: stdout
listing **zero** models (no credentials, or every configured provider
unreachable) means "can't even see a catalogue" -- classified the SAME as a
hard fetch failure (``fetch_models`` returns ``(None, reason)``, never
``([], None)``), so it flows through the exact same ``models is None`` branch
``providers.ollama``'s own callers already treat as TRANSIENT. Stdout listing
models with the requested id absent is a genuinely bad tag -- PERMANENT,
``ops.ask``. Swapping these burns the attempts ledger on a mere outage, or
silently retries a typo forever.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

TIMEOUT_S = 15.0

# The columns of `pi --list-models`' fixed-width table, in order (verified
# against the installed 0.79.2's real stdout, see
# tests/fixtures/pi_list_models_table.txt). Column widths aren't constant --
# each is padded to the widest cell (header included) for that one
# invocation -- so parsing splits on runs of 2+ spaces rather than fixed byte
# offsets.
_HEADER_COLUMNS = ("provider", "model", "context", "max-out", "thinking", "images")
_COLUMN_SPLIT_RE = re.compile(r"\s{2,}")

# Verified real stdout for a credential-less (or fully unauthenticated)
# install: exit 0, no table at all -- just this message. Matched as a
# substring so a version-specific rewording of the surrounding sentence still
# hits it as long as pi keeps saying "No models available".
_NO_MODELS_MARKER = "No models available"


def fetch_models(
    pi_agent_dir: Path | str | None = None, *, run: Callable | None = None
) -> tuple[list[dict] | None, str | None]:
    """List every model pi can currently resolve via ``pi --list-models
    --offline``, run under *pi_agent_dir* (``$PI_CODING_AGENT_DIR``, PI-4's
    ``store.pi_agent_dir(home)`` -- isolates the probe from a developer's real
    ``~/.pi``) if given. ``--offline`` disables pi's own startup network
    operations (a version check), so this never depends on connectivity
    beyond what listing already requires.

    Returns ``(models, None)`` on success, one dict per table row (raw string
    values, keyed by the header names above) -- or ``(None, reason)`` on ANY
    failure: missing binary, non-zero exit, a hung process past
    ``TIMEOUT_S``, unparseable stdout, or a stdout that lists zero models
    (credentials/endpoint not resolved -- see the module docstring for why
    that collapses to the SAME failure shape as an outright fetch error,
    not an empty success list). Never raises.

    ``run`` is the injectable subprocess boundary (``health.check_pi_version``'s
    own established pattern for a pi subprocess call in this codebase) --
    defaults to ``subprocess.run``; tests substitute a fake to assert argv/env
    or simulate each failure mode without shelling a real ``pi``.

    T-59: this is currently the ONLY argv maestro constructs for the `pi`
    executable that could plausibly run past startup (``health.check_pi_version``'s
    own ``pi --version`` probe exits before extensions or tools ever load, so
    it's out of scope -- see that function's docstring) -- so it carries
    ``pi_guard.spawn_argv``'s guard flags whenever *pi_agent_dir* is given,
    same as any future real spawn backend would. Installing the guard is
    best-effort: a failure there must never break this function's own
    never-raises contract, so it degrades to the plain probe argv instead.
    """
    run = run or subprocess.run
    argv = ["pi", "--list-models", "--offline"]
    env = None
    if pi_agent_dir is not None:
        import os
        env = dict(os.environ)
        env["PI_CODING_AGENT_DIR"] = str(pi_agent_dir)
        try:
            from .. import pi_guard
            argv += pi_guard.spawn_argv(Path(pi_agent_dir))
        except OSError:
            pass
    try:
        proc = run(argv, capture_output=True, text=True, timeout=TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        return None, f"pi --list-models timed out after {TIMEOUT_S}s"
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return None, f"pi --list-models exited {proc.returncode}: {detail}"

    stdout = proc.stdout or ""
    if _NO_MODELS_MARKER in stdout:
        return None, "no models available (pi has no resolved credentials/endpoint)"

    models = _parse_table(stdout)
    if models is None:
        return None, f"unparseable pi --list-models output: {stdout[:200]!r}"
    if not models:
        return None, "no models available (pi has no resolved credentials/endpoint)"
    return models, None


def _parse_table(stdout: str) -> list[dict] | None:
    """Parse the fixed-width table (see ``_HEADER_COLUMNS``). Returns ``None``
    when no header row is found at all (unparseable output -- pi's format
    changed, or this isn't a table); returns ``[]`` when a header is found
    but every following line fails to split into the expected column count
    (e.g. the table is empty)."""
    lines = [ln.rstrip() for ln in stdout.splitlines() if ln.strip()]
    header_idx = None
    for i, ln in enumerate(lines):
        if tuple(_COLUMN_SPLIT_RE.split(ln.strip())) == _HEADER_COLUMNS:
            header_idx = i
            break
    if header_idx is None:
        return None
    rows = []
    for ln in lines[header_idx + 1:]:
        cells = _COLUMN_SPLIT_RE.split(ln.strip())
        if len(cells) != len(_HEADER_COLUMNS):
            continue  # tolerate a stray non-table line (a warning, a banner)
        rows.append(dict(zip(_HEADER_COLUMNS, cells)))
    return rows


def model_names(models: list[dict]) -> list[str]:
    """Bare model ids (the ``model`` column) with no ``provider/`` prefix --
    matching what a spec's ``runner_model:`` line and ``[maestro]
    runner_model`` store verbatim. The prefix is composed in the session
    backend and nowhere else (the ``ollama/`` precedent, ``sessions.py``)."""
    return [m["model"] for m in models if m.get("model")]


def verdict_for_model(
    models: list[dict] | None, reason: str | None, model_name: str
) -> tuple[str, str | None]:
    """The three-valued verdict for one ``model_name`` against a
    ``fetch_models()`` result -- ``"ok"`` (listed), ``"missing"`` (pi is
    reachable and has a catalogue, but this id isn't in it -- a human must fix
    the spec's ``runner_model:`` or the board's ``[runner.pi]`` config), or
    ``"unreachable"`` (no catalogue at all -- ``models`` is ``None`` or empty,
    same TRANSIENT treatment OC-2 already gives ``providers.ollama``'s own
    ``None`` case). That distinction is load-bearing for the transient-vs-
    permanent split: unreachable is worth retrying, missing needs a human."""
    if not models:
        return "unreachable", reason
    names = model_names(models)
    if model_name in names:
        return "ok", None
    return "missing", f"{model_name!r} not found among pi's configured models ({sorted(set(names))})"
