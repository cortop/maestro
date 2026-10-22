"""`maestro doctor` flags a dispatcher hook that errors on every recent sweep."""
from __future__ import annotations

import json

from maestro import dispatcher, health
from tests.conftest import run_doctor


def _write_ledger(home, records):
    path = dispatcher.dispatch_ledger_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _check(home):
    _code, payload = run_doctor(home)
    return next(c for c in payload["checks"] if c["name"] == "hook_errors")


def _sweeps(n, err="HTTPError: 401"):
    return [{"ts": f"t{i}", "hook_errors": {"sync_external_sources": err}, "decisions": {}}
            for i in range(n)]


def test_warns_persistent_hook_error(home):
    _write_ledger(home, _sweeps(health._HOOK_ERROR_STREAK + 3))
    check = _check(home)
    assert check["status"] == "warn"
    assert "sync_external_sources" in check["detail"]
    assert "HTTPError: 401" in check["detail"]


def test_silent_when_recovered_or_absent(home):
    n = health._HOOK_ERROR_STREAK
    assert _check(home)["status"] == "ok"            # missing file
    _write_ledger(home, [])
    assert _check(home)["status"] == "ok"            # empty file
    recovered = _sweeps(n) + [{"ts": "x", "hook_errors": {}, "decisions": {}}]
    _write_ledger(home, recovered)
    assert _check(home)["status"] == "ok"            # latest sweep clean
    flaky = _sweeps(n)
    flaky[n // 2]["hook_errors"] = {}
    _write_ledger(home, flaky)
    assert _check(home)["status"] == "ok"            # intermittent
