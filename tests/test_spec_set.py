"""T-179: `maestro spec-set` / `ops.set_spec_fields`."""

from maestro import cli, dispatcher as disp, event_log, store
from maestro.config import load
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase
from conftest import seed_phase, seed_ticket


def _spec(home, key):
    return store.spec_path(home, key).read_bytes()


def _run(home, *argv):
    return cli.main(["--home", str(home), "spec-set", *argv])


def _seed(home, key, text):
    seed_ticket(home, key, key, phase="ready")
    store.atomic_write(store.spec_path(home, key), text)


def test_spec_set_depends_on_changes_one_line(seeded_home):
    home = seeded_home
    before = _spec(home, "T-5").decode().splitlines()
    n_events = len(event_log.read(home, "T-5"))
    assert _run(home, "T-5", "--depends-on", "T-3") == 0
    after = _spec(home, "T-5").decode().splitlines()
    assert len(after) - len(before) == 1 and after.count("dependsOn: [T-3]") == 1 and \
        [l for l in before if l not in after] == []
    assert len(event_log.read(home, "T-5")) == n_events


def test_spec_set_priority(home):
    text = ("# T-1\n\npriority: 3\napproval_tier: 2\ndependsOn: []\n\n"
            "## Acceptance criteria\n- [ ] ok\n")
    _seed(home, "T-1", text)
    assert _run(home, "T-1", "--priority", "0") == 0
    assert _spec(home, "T-1").decode() == text.replace("priority: 3", "priority: 0")
    good = _spec(home, "T-1")
    assert _run(home, "T-1", "--priority", "-1") != 0
    assert _run(home, "T-1", "--priority", "abc") != 0
    assert _spec(home, "T-1") == good


def test_spec_set_refuses_cycle_self_and_unknown(seeded_home, capsys):
    home = seeded_home
    assert _run(home, "T-3", "--depends-on", "T-5") == 0
    for key, dep, word in (("T-5", "T-3", "cycle"), ("T-5", "T-5", "itself"),
                           ("T-5", "T-999", "unknown")):
        before = _spec(home, key)
        capsys.readouterr()
        assert _run(home, key, "--depends-on", dep) != 0
        assert word in capsys.readouterr().err
        assert _spec(home, key) == before


def test_spec_set_wakes_ticket(home):
    seed_phase(home, "T-7", Phase.DEGRADED)
    cfg = load(home)

    before = disp.dispatch(cfg, DryRunSessions(), now=1_000_000.0)
    assert "T-7" not in _due_keys(before)
    assert _run(home, "T-7", "--priority", "1") == 0
    after = disp.dispatch(cfg, DryRunSessions(), now=1_000_100.0)
    assert ("T-7", "spec-changed") in _due_pairs(after)


def _due_pairs(report):
    return [(d[0], d[1]) if isinstance(d, (tuple, list)) else (d.key, d.reason)
            for d in report.due]


def _due_keys(report):
    return [k for k, _ in _due_pairs(report)]
