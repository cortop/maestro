"""Doctor-check rows for FleetScreen: worst-first ordering, culprit keys, and the remedy map."""
from __future__ import annotations

from collections.abc import Callable

_ORDER = {"fail": 0, "warn": 1, "ok": 2}

# Remedy ids a `_CheckModal` can return; FleetScreen maps each to an action.
RETRY, DISCARD, LOGS, RELEASE, BACKUP_NOW, ADD_AC, SUGGEST_ACS, SPEC = (
    "retry", "discard", "logs", "release", "backup-now", "add-ac", "suggest-acs", "spec")


def sort_checks(checks: list[dict]) -> list[dict]:
    """Fail, then warn, then ok; by name within a status."""
    return sorted(checks, key=lambda c: (_ORDER.get(c.get("status"), 3), str(c.get("name"))))


def counts_line(checks: list[dict]) -> str:
    n = {s: sum(1 for c in checks if c.get("status") == s) for s in _ORDER}
    return f"{n['fail']} fail · {n['warn']} warn · {n['ok']} ok"


def _real(keys) -> list[str]:
    return [k for k in keys if k]


def _dead_letters(c: dict) -> list[str]:
    # `rejected-*` stems are never-minted create requests, not tickets.
    return [k for k in sorted(c.get("ages_s") or {}) if not k.startswith("rejected-")]


def _depends_on(c: dict) -> list[str]:
    keys = [m.get("key") for m in c.get("missing") or []]
    for cycle in c.get("cycles") or []:
        keys.extend(cycle)
    return list(dict.fromkeys(_real(keys)))


def _unknown_repo(c: dict) -> list[str]:
    return [u["key"] for u in c.get("unknown") or []
            if u.get("key") and not str(u["key"]).startswith("scheduled:")]


# check name -> culprit keys, in that check's own field.
_CULPRITS: dict[str, Callable[[dict], list[str]]] = {
    "dead_letters": _dead_letters,
    "claim_age": lambda c: _real([c.get("oldest_key")]),
    "claim_no_output": lambda c: _real([c.get("stale_key")]),
    "missing_acs": lambda c: _real(c.get("keys") or []),
    "depends_on": _depends_on,
    "unknown_repo_bindings": _unknown_repo,
    "burn": lambda c: _real(c.get("flagged") or []),
    "watchdog_loops": lambda c: sorted(c.get("counts") or {}),
    "phantom_keys": lambda c: _real(c.get("keys") or []),
    "unresolvable_spec_hints": lambda c: _real(c.get("keys") or []),
    "ac_annotation_parse": lambda c: list(dict.fromkeys(
        _real(f.get("key") for f in c.get("flagged") or []))),
}

# check name -> remedies offered per culprit key (`g` is always available separately).
_REMEDIES: dict[str, list[tuple[str, str]]] = {
    "dead_letters": [(RETRY, "Retry"), (DISCARD, "Discard")],
    "claim_age": [(LOGS, "Logs"), (RELEASE, "Release")],
    "claim_no_output": [(LOGS, "Logs"), (RELEASE, "Release")],
    "missing_acs": [(ADD_AC, "Add AC"), (SUGGEST_ACS, "Suggest ACs")],
    "depends_on": [(SPEC, "Spec")],
    "unknown_repo_bindings": [(SPEC, "Spec")],
}

# Remedies with no culprit key.
_KEYLESS: dict[str, list[tuple[str, str]]] = {"backup_age": [(BACKUP_NOW, "Back up now")]}

# Remedies that only apply to a degraded ticket.
DEGRADED_ONLY = (RETRY, DISCARD)


def culprit_keys(check: dict) -> list[str]:
    fn = _CULPRITS.get(str(check.get("name")))
    return fn(check) if fn else []


def keyless_remedies(check: dict) -> list[tuple[str, str]]:
    return list(_KEYLESS.get(str(check.get("name")), []))


def key_remedies(check: dict) -> list[tuple[str, str]]:
    return list(_REMEDIES.get(str(check.get("name")), []))
