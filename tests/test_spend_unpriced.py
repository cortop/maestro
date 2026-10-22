"""T-194: pi spend visibility -- unpriced-model doctor warning, unpriced session
counters in the spend meter, and pi logs in burn.per_key_spend. Drives the real
doctor verb and real dispatcher sweeps over a temp home."""
import json

from maestro import burn, spend
from maestro import dispatcher as disp
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase
from conftest import run_doctor, seed_phase

NOW = 1_000_000


def _write_session(home, key, epoch, suffix, records):
    path = home / "agent-logs" / key / f"reconcile-{key}-{epoch:.6f}.{suffix}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _message_end(total_cost, *, input_tokens=1200, output_tokens=300):
    """A pi `message_end` in pi's real shape (usage with camelCase cache fields)."""
    return {"type": "message_end",
            "message": {"role": "assistant", "stopReason": "stop", "provider": "zai",
                        "model": "glm-5.2",
                        "usage": {"input": input_tokens, "output": output_tokens,
                                  "cacheRead": 40000, "cacheWrite": 0,
                                  "totalTokens": input_tokens + output_tokens + 40000,
                                  "cost": {"input": 0, "output": 0, "cacheRead": 0,
                                           "cacheWrite": 0, "total": total_cost}}}}


def _pi_records(*costs):
    return ([{"type": "session", "version": 3, "id": "s"}, {"type": "agent_start"}]
            + [_message_end(c) for c in costs]
            + [{"type": "agent_end", "messages": [], "willRetry": False}])


def _spawned(home, cfg, key="T-1"):
    cfg.min_spawn_interval = 0
    seed_phase(home, key, Phase.READY)
    assert key in disp.dispatch(cfg, DryRunSessions(), now=NOW).spawned


def _doctor_check(home):
    _, out = run_doctor(home)
    return next(c for c in out["checks"] if c["name"] == "pi_unpriced_models")


def _write_config(home, models_toml):
    (home / "config.toml").write_text(
        '[runner.pi]\nphases = ["implementing"]\n' + models_toml, encoding="utf-8")


def test_doctor_warns_on_unpriced_pi_model(home):
    _write_config(home, '[runner.pi.models."glm-5.2"]\ncontext_window = 200000\n')
    check = _doctor_check(home)
    assert check["status"] == "warn"
    assert check["models"] == ["glm-5.2"]

    _write_config(home, '[runner.pi.models."glm-5.2"]\ncontext_window = 200000\n'
                        '[runner.pi.models."glm-5.2".cost]\ninput = 0.6\noutput = 2.2\n')
    assert _doctor_check(home)["status"] == "ok"


def test_unpriced_pi_sessions_counted(home, cfg):
    _spawned(home, cfg)
    _write_session(home, "T-1", NOW, "pi", _pi_records(0, 0))
    st = spend.probe(cfg, NOW + 10)
    assert st["total_usd"] == 0.0
    assert st["unpriced_sessions"] == 1
    assert st["unpriced_tokens"] == 2 * (1200 + 300 + 40000)
    # A re-probe with no new bytes counts nothing twice.
    assert spend.probe(cfg, NOW + 20)["unpriced_sessions"] == 1
    assert spend.status(cfg, NOW + 20)["unpriced_sessions"] == 1


def test_per_key_spend_reads_pi_logs(home, cfg):
    _spawned(home, cfg)
    _write_session(home, "T-1", NOW, "pi", _pi_records(0.5, 0.25))
    assert burn.per_key_spend(home, NOW + 10) == {"T-1": 0.75}
    st = spend.probe(cfg, NOW + 10)
    assert st["total_usd"] == 0.75
    assert st.get("unpriced_sessions", 0) == 0


def test_opencode_not_flagged(home, cfg):
    _spawned(home, cfg)
    _write_session(home, "T-1", NOW, "opencode", [
        {"type": "step_finish", "part": {"tokens": {"input": 900, "output": 100},
                                         "cost": 0}}])
    st = spend.probe(cfg, NOW + 10)
    assert st.get("unpriced_sessions", 0) == 0
    assert st.get("unpriced_tokens", 0) == 0
