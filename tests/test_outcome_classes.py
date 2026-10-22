"""T-210: no-progress Failed carries the provider state and terminal reasons."""
import json

from conftest import seed_phase

from maestro import dispatcher as disp
from maestro import event_log, health, steplog, store
from maestro.statemachine import Phase


def _error_log(home, key, epoch, message):
    path = store.session_stream_path(home, key, f"reconcile-{key}-{epoch:.6f}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "result", "subtype": "error_during_execution",
                                "is_error": True, "api_error_status": None,
                                "result": message}) + "\n", encoding="utf-8")


def _trip(cfg, home, monkeypatch, *, offline):
    cfg.max_spawn_attempts = 5
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    for i in range(5):
        _error_log(home, "T-1", 100.0 + i, "API Error: connect ECONNREFUSED 127.0.0.1:443")
    monkeypatch.setattr(health, "_default_provider_probe",
                        lambda host: (False, "offline") if offline else (True, "ok"))
    attempts = {"T-1": {"seq": 0, "count": 5}}
    assert disp._allow_spawn(cfg, "T-1", 0, attempts) is False
    return next(e for e in event_log.read(home, "T-1") if e["type"] == "Failed")["payload"]


def test_no_progress_fail_carries_provider_kind(home, cfg, monkeypatch):
    payload = _trip(cfg, home, monkeypatch, offline=True)
    assert payload["kind"] == "provider"
    assert payload["state"] == "no_network"


def test_fail_text_lists_terminal_reasons(home, cfg, monkeypatch):
    payload = _trip(cfg, home, monkeypatch, offline=False)
    assert payload["error"].endswith("(ECONNREFUSED x5)")


def test_outcome_class_vocabulary():
    def err(text=None, status=None):
        return {"outcome": "error", "result": {"result": text, "api_error_status": status}}

    assert steplog.outcome_class({"outcome": "success"}) == "success"
    assert steplog.outcome_class({"outcome": "rate_limited"}) == "rate_limited"
    assert steplog.outcome_class({"outcome": "crashed", "result": {"tail": "x"}}) == "crashed"
    assert steplog.outcome_class(err("connect ECONNREFUSED")) == "error:econnrefused"
    assert steplog.outcome_class(err("getaddrinfo ENOTFOUND api")) == "error:enotfound"
    assert steplog.outcome_class(err("request timed out")) == "error:timeout"
    assert steplog.outcome_class(err("nope", 401)) == "error:auth"
    assert steplog.outcome_class(err("nope", 503)) == "error:server"
    # pi: error text lives in result.result too
    assert steplog.outcome_class(
        {"outcome": "error", "result": {"stopReason": "error", "result": "ETIMEDOUT"}}
    ) == "error:timeout"
    assert steplog.outcome_class(err("something odd")) == "error:unclassified"
    assert steplog.outcome_class({"outcome": "error", "result": None}) == "error:unclassified"
    assert steplog.outcome_class({"outcome": "weird"}) == "error:unclassified"
    for sample in (err("x"), err("ECONNREFUSED"), {"outcome": "running"}):
        assert steplog.outcome_class(sample) in steplog.OUTCOME_CLASSES
