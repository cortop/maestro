"""T-212: `maestro scorecard tickets` -- per-repo/month outcome figures."""
import json
import math

from maestro import cli, outcomes, store
from maestro.config import Config


def _write(home, key, rows, repo="default"):
    """Write raw events with fixed timestamps (rows: (ts, type, payload))."""
    store.atomic_write(store.spec_path(home, key), f"# {key}\nrepo: {repo}\n\n## Acceptance criteria\n- [ ] ok\n")
    lines = []
    for i, (ts, typ, pl) in enumerate(rows, 1):
        lines.append(json.dumps({"seq": i, "ts": ts, "key": key, "actor": "t", "type": typ,
                                 "step_id": None, "fencing_token": i, "payload": pl}))
    p = store.events_path(home, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")


def _ph(ts, phase, reason=""):
    return (ts, "PhaseChanged", {"phase": phase, "reason": reason})


def _board(home):
    # T-1: clean merge, QA pass first time, one un-QA'd CI round, 5h cycle.
    _write(home, "T-1", [
        ("2026-09-01T10:00:00+00:00", "TicketCreated", {"title": "a"}),
        _ph("2026-09-01T10:30:00+00:00", "implementing"),
        _ph("2026-09-01T11:00:00+00:00", "qa"),
        ("2026-09-01T11:10:00+00:00", "AcQaVerdict", {"ac_hash": "h", "verdict": "pass"}),
        _ph("2026-09-01T11:20:00+00:00", "awaiting-ci"),
        _ph("2026-09-01T12:00:00+00:00", "implementing", "CI failing: lint"),
        _ph("2026-09-01T12:30:00+00:00", "awaiting-ci"),
        ("2026-09-01T15:00:00+00:00", "PrUpdated", {"merged": True}),
        ("2026-09-01T15:00:01+00:00", "Finalized", {}),
        _ph("2026-09-01T15:00:02+00:00", "done"),
    ])
    # T-2: QA fails first, two same-tick duplicate review routes, merged in 48h.
    _write(home, "T-2", [
        ("2026-09-02T10:00:00+00:00", "TicketCreated", {"title": "b"}),
        _ph("2026-09-02T10:30:00+00:00", "implementing"),
        _ph("2026-09-02T11:00:00+00:00", "qa"),
        ("2026-09-02T11:10:00+00:00", "AcQaVerdict", {"ac_hash": "h", "verdict": "fail"}),
        _ph("2026-09-02T11:20:00+00:00", "implementing", "qa failed AC 1"),
        _ph("2026-09-02T12:00:00+00:00", "qa"),
        _ph("2026-09-02T13:00:00+00:00", "in-review"),
        ("2026-09-02T14:00:00+00:00", "ReviewFeedbackReceived", {"comment_id": "1", "author": "alice"}),
        ("2026-09-02T14:00:00+00:00", "ReviewFeedbackReceived", {"comment_id": "2", "author": "ci[bot]"}),
        ("2026-09-02T14:00:00+00:00", "ReviewFeedbackReceived", {"comment_id": "3"}),
        _ph("2026-09-02T14:00:00+00:00", "implementing", "changes requested: fix"),
        _ph("2026-09-02T14:00:00+00:00", "implementing", "review comment: fix"),
        _ph("2026-09-02T15:00:00+00:00", "awaiting-ci"),
        ("2026-09-04T10:00:00+00:00", "PrUpdated", {"merged": True}),
    ])
    # T-3: still open, other repo, later month.
    _write(home, "T-3", [
        ("2026-10-05T10:00:00+00:00", "TicketCreated", {"title": "c"}),
        _ph("2026-10-05T10:30:00+00:00", "implementing"),
    ], repo="other")


def _cfg(home):
    c = Config(home=home)
    c.review_noise_authors = []
    return c


def _groups(home, **kw):
    out = outcomes.scorecard(_cfg(home), **kw)
    return {(g["repo"], g["month"]): g for g in out["groups"]}


def test_metrics_have_n_and_interval(home, capsys):
    _board(home)
    assert cli.main(["--home", str(home), "scorecard", "tickets"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {(g["repo"], g["month"]) for g in out["groups"]} == {("default", "2026-09"), ("other", "2026-10")}
    for g in out["groups"]:
        figs = [g["terminal_merged"], g["first_pass_qa"], g["unqad_rounds_before_merge"],
                g["cycle_time"], g["reviews_bot"], *g["rounds_by_cause"].values()]
        for f in figs:
            assert {"k", "n", "excluded", "wilson95"} <= set(f)
            if f["n"]:
                lo, hi = f["wilson95"]
                assert 0 <= lo <= f["rate"] <= hi <= 1
            else:
                assert f["wilson95"] is None
    assert math.isclose(outcomes.wilson(1, 2)[0], 0.0945, abs_tol=1e-3)


def test_same_tick_routes_once(home):
    _board(home)
    g = _groups(home)[("default", "2026-09")]
    review = g["rounds_by_cause"]["review"]
    assert review["rounds"] == 1 and review["k"] == 1


def test_pinned_cutoff_numbers(home):
    _board(home)
    cutoff = outcomes.datetime.fromisoformat("2026-09-02T12:00:00+00:00")
    g = _groups(home, cutoff=cutoff)[("default", "2026-09")]
    assert g["tickets"] == 2
    # T-1 merged; T-2 not yet terminal at the cutoff -> 1 of 1, 1 excluded.
    assert (g["terminal_merged"]["k"], g["terminal_merged"]["n"], g["terminal_merged"]["excluded"]) == (1, 1, 1)
    assert (g["first_pass_qa"]["k"], g["first_pass_qa"]["n"]) == (1, 2)
    assert g["rounds_by_cause"]["qa"]["rounds"] == 1
    assert g["cycle_time"]["median_hours"] == 5.0
    full = _groups(home)
    g = full[("default", "2026-09")]
    assert (g["terminal_merged"]["k"], g["terminal_merged"]["n"]) == (2, 2)
    assert (g["first_pass_qa"]["k"], g["first_pass_qa"]["n"]) == (1, 2)
    assert (g["unqad_rounds_before_merge"]["k"], g["unqad_rounds_before_merge"]["rounds"]) == (2, 2)
    assert g["cycle_time"]["median_hours"] == 26.5
    assert (g["cycle_time"]["k"], g["cycle_time"]["n"]) == (1, 2)
    assert (g["reviews_bot"]["k"], g["reviews_bot"]["n"], g["reviews_bot"]["excluded"]) == (1, 2, 1)
    assert g["reviews_bot"]["wilson95"] == outcomes.wilson(1, 2)
    o = full[("other", "2026-10")]
    assert (o["terminal_merged"]["n"], o["terminal_merged"]["excluded"]) == (0, 1)


def test_read_only(home):
    _board(home)
    before = {p: p.read_bytes() for p in (home / "events").glob("*.jsonl")}
    assert cli.main(["--home", str(home), "scorecard", "tickets"]) == 0
    after = {p: p.read_bytes() for p in (home / "events").glob("*.jsonl")}
    assert before == after
    assert not (home / "derived" / "labels").exists()
