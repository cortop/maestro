"""T-199: `maestro doctor` flags diff gates that are configured but can never run."""
from conftest import run_doctor, seed_ticket

from maestro import store
from maestro.config import Config
from maestro import health


def _check(home, repos):
    return health.check_dark_gates(Config(home=home, repos=repos), now=1_000_000)


def _annotate(home, key):
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\n\n## Intent\nx\n\n## Acceptance criteria\n"
                       "- [ ] works (check: true)\n")


def test_warns_allowed_paths_without_test_command(home, tmp_path):
    result = _check(home, {"web": {"path": str(tmp_path), "default": True,
                                   "allowed_paths": ["src/**"]}})
    assert result["status"] == "warn"
    assert result["allowed_paths"][0]["repo"] == "web"
    assert "web" in result["detail"]


def test_warns_annotated_acs_without_test_command(home, tmp_path):
    seed_ticket(home, "T-1", "t")
    _annotate(home, "T-1")
    result = _check(home, {"web": {"path": str(tmp_path), "default": True}})
    assert result["status"] == "warn"
    assert result["annotated_acs"] == [{"key": "T-1", "repo": "web"}]


def test_silent_with_test_command(home, tmp_path):
    seed_ticket(home, "T-1", "t")
    _annotate(home, "T-1")
    result = _check(home, {"web": {"path": str(tmp_path), "default": True,
                                   "allowed_paths": ["src/**"],
                                   "test_command": "pytest -q"}})
    assert result["status"] == "ok"


def test_doctor_verb_reports_dark_gates(home):
    _code, payload = run_doctor(home)
    assert "dark_gates" in {c["name"] for c in payload["checks"]}
