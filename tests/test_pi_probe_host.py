"""T-204: `[runner.pi] probe_host` steers the pi provider-availability probe, and
an unmapped pi provider is reported unprobed instead of probed at Anthropic."""
import pytest

from maestro import config, health, store

from conftest import seed_ticket
from test_provider_availability import _write_pi_session


def _pi_streak(home, provider):
    seed_ticket(home, "T-1", "x", phase="implementing")
    for epoch in (100.0, 200.0, 300.0):
        _write_pi_session(home, "T-1", epoch, "error", provider=provider)


def test_configured_probe_host_used(cfg, home):
    _pi_streak(home, "acme-gateway")
    cfg.raw = {"runner": {"pi": {"probe_host": "gateway.internal.test"}}}
    captured = []

    def _probe(host):
        captured.append(host)
        return True, "reachable"

    result = health.check_provider_availability(cfg, 1000, probe=_probe)
    assert captured == ["gateway.internal.test"]
    assert result["state"] == "erroring"


def test_unmapped_provider_not_probed_elsewhere(cfg, home):
    _pi_streak(home, "acme-gateway")

    def _boom(host):
        raise AssertionError(f"must not probe {host}")

    result = health.check_provider_availability(cfg, 1000, probe=_boom)
    assert result["state"] == "unprobed"
    assert result["status"] == "warn"
    assert "probe_host" in result["detail"]


@pytest.mark.parametrize("bad", ["42", "true", "['h']", '""'])
def test_probe_host_validated(home, bad):
    (home / "config.toml").write_text(f"[runner.pi]\nprobe_host = {bad}\n", encoding="utf-8")
    with pytest.raises(store.MaestroError, match="probe_host"):
        config.load(home)
