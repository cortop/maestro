"""Per-repo GitHub credential resolution — ``[repos.<name>] gh_account`` /
``token_env``.

Two disjoint ``gh`` accounts on one machine cannot both work through the single
active ``gh auth`` selector (``~/.config/gh/hosts.yml``'s ``user:`` field) — a
board binding repos owned by different accounts needs to resolve a credential
PER repo, not rely on whichever account a human last ran ``gh auth switch``
as. ``token_env`` wins when both fields are set on one binding (the human's
GA-17 schema answer): it mirrors the existing ``providers/jira.py``
``token_env`` / ``providers/linear.py`` ``api_key_env`` precedent exactly, and
stays deterministic under launchd (no keyring dependence), while
``gh_account`` still works for interactive/local use where the token is
already sitting in the keychain.

Fail-closed, always: :func:`resolve` never raises and never falls back to the
ambient environment. A binding with a credential configured that can't be
resolved (an unset/empty ``token_env`` variable, or a non-zero ``gh auth
token --user``) comes back ``ok=False`` — callers (``dispatcher``'s spawn
site and its own ``sync_vcs`` poll) must treat that as "do not proceed",
never as "use whatever `gh` account happens to be active". A binding with
NEITHER field set resolves ``ok=True, env=None`` — "nothing configured",
today's ambient behavior, unchanged.

Never carries the secret in anything but ``env``: :func:`credential_label`
reports only the *identifier* (an env var name or an account login), safe to
print in ``maestro env --key`` / logs / events — the token value itself is
never returned from any function here except inside the ``env`` overlay
callers merge straight into a child process's environment.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass
class CredentialResolution:
    ok: bool
    # The overlay to merge into a child process's env, e.g. {"GH_TOKEN": "..."}.
    # None means "nothing configured" (ok=True) -- callers merge nothing extra,
    # today's ambient behavior. Never set when ok=False.
    env: dict[str, str] | None = None
    # Human-legible, secret-free identifier -- "token_env:VAR" or
    # "gh_account:login" -- set whenever a field IS configured, even if it
    # failed to resolve (so `maestro env --key` can still name what's bound).
    label: str | None = None
    error: str | None = None  # set only when ok is False


def credential_label(gh_account: str | None, token_env: str | None) -> str | None:
    """The identifier a binding's credential fields resolve to, WITHOUT
    resolving or reading anything -- no subprocess, no env lookup. Zero-cost,
    so it's safe on the hot path (``maestro env --key`` runs at the top of
    every reconciler phase preamble). ``token_env`` wins when both are set,
    mirroring :func:`resolve`'s own precedence. None when neither is set.
    """
    if token_env:
        return f"token_env:{token_env}"
    if gh_account:
        return f"gh_account:{gh_account}"
    return None


def resolve(gh_account: str | None, token_env: str | None, *,
           run=subprocess.run) -> CredentialResolution:
    """Resolve one binding's ``gh_account``/``token_env`` into a ``GH_TOKEN``
    env overlay. ``token_env`` wins when both are set. Fails closed (``ok =
    False``, no ``env``) rather than ever falling back to the ambient ``gh``
    account -- see the module docstring.
    """
    label = credential_label(gh_account, token_env)
    if token_env:
        value = os.environ.get(token_env)
        if not value:
            return CredentialResolution(ok=False, label=label,
                                        error=f"token_env {token_env!r} is unset or empty")
        return CredentialResolution(ok=True, env={"GH_TOKEN": value}, label=label)
    if gh_account:
        try:
            p = run(["gh", "auth", "token", "--user", gh_account],
                   capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            return CredentialResolution(ok=False, label=label,
                                        error=f"gh auth token --user {gh_account} failed: "
                                              f"{type(e).__name__}: {e}")
        token = (p.stdout or "").strip()
        if p.returncode != 0 or not token:
            return CredentialResolution(ok=False, label=label,
                                        error=(p.stderr or "gh auth token failed").strip())
        return CredentialResolution(ok=True, env={"GH_TOKEN": token}, label=label)
    return CredentialResolution(ok=True, env=None, label=None)


def resolve_cached(gh_account: str | None, token_env: str | None, cache: dict, *,
                   run=subprocess.run) -> CredentialResolution:
    """:func:`resolve`, memoized in *cache* by ``(gh_account, token_env)`` --
    the caller-owned dict a dispatcher sweep passes in so N keys bound to the
    same repo resolve the token once, not once per key."""
    cache_key = (gh_account, token_env)
    if cache_key not in cache:
        cache[cache_key] = resolve(gh_account, token_env, run=run)
    return cache[cache_key]
