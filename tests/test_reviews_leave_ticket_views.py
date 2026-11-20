"""T-301: reviews stay off every ticket surface (board, counts, graph, scorecard, status, queues)."""
from __future__ import annotations

import io
import json
import tarfile

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_review_ticket, seed_ticket  # noqa: E402
from maestro import cli, depgraph, outcomes, projection, store  # noqa: E402
from maestro import backup
from maestro import dispatcher as disp, snapshot as snap_mod  # noqa: E402
from maestro.config import Config
from maestro.sessions import DryRunSessions  # noqa: E402
from maestro.view import board as board_view  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


@pytest.fixture
def mixed(home):
    seed_ticket(home, "T-1", "plain ticket", phase="ready")
    seed_review_ticket(home, "T-60")
    return home


def _json_of(capsys, argv):
    capsys.readouterr()
    cli.main(argv)
    return json.loads(capsys.readouterr().out)


def test_tickets_page_header_and_now_card(mixed):
    with TestClient(create_app(mixed)) as c:
        html = c.get("/tickets?filter=all").text
        assert "T-1" in html and "/t/T-60" not in html and "T-60<" not in html
        for name in board_view.FILTER_NAMES:
            assert "T-60" not in c.get(f"/tickets?filter={name}").text
        view = board_view.board(mixed, Config(home=mixed), "all")
        assert [r.key for r in view.rows] == ["T-1"] and view.counts["all"] == 1
        assert "2 tickets" not in html
        now = c.get("/").text
        assert 'href="/reviews/T-60"' in now.split('data-kind="review"')[1]
        assert "T-60" in c.get("/reviews").text


def test_review_in_awaiting_ci_matches_neither_filter(home):
    seed_ticket(home, "T-1", "own pr", phase="awaiting-ci")
    seed_review_ticket(home, "T-60", brief=False, phase="awaiting-ci")
    snaps = projection.load_snapshots(home)
    assert [s.key for s in board_view.select(home, snaps, "review")] == ["T-1"]
    pred = projection.parse_query("is:review")
    assert [s.key for s in snaps if pred(s, "", 0)] == ["T-1"]


def test_status_json_reports_review_apart(mixed, capsys):
    out = _json_of(capsys, ["--home", str(mixed), "status"])
    assert out["tickets"] == 1 and out["by_phase"] == {"ready": 1}
    assert out["reviews"] == {"open": 1, "needs_you": 1, "by_status": {"brief-ready": 1}}
    assert all(k != "T-60" for k, *_ in out["needs_you"])


def test_workstate_and_needs_you(mixed):
    files = projection.render(mixed)
    ws, ny = files["WORKSTATE.md"], files["NEEDS-YOU.md"]
    assert "| T-60 " not in ws and "| T-1 " in ws and "**1 tickets**" in ws
    assert "1 open reviews, 1 need you" in ws and "`maestro reviews`" in ws
    assert "## Reviews" in ny and "T-60" in ny.split("## Reviews")[1]
    assert "T-60" not in ny.split("## Reviews")[0]
    assert "## Questions" not in ny and "Nothing is waiting" not in ny


def test_queue_graph_scorecard_exclude_review(mixed):
    assert [r["key"] for r in projection.open_question_queue(mixed)] == []
    assert set(depgraph.build(mixed).nodes) == {"T-1"}
    card = outcomes.scorecard(Config(home=mixed))
    assert card["tickets"] == 1


def test_activity_chip_link_and_palette_jump(mixed):
    with TestClient(create_app(mixed)) as c:
        rows = c.get("/activity/rows").text
        t60 = [r for r in rows.split("</tr>") if 'data-key="T-60"' in r]
        assert t60 and all('href="/reviews/T-60"' in r and "review</span>" in r for r in t60)
        t1 = [r for r in rows.split("</tr>") if 'data-key="T-1"' in r]
        assert t1 and all('href="/t/T-1"' in r and "chip-kind" not in r for r in t1)
        pal = c.get("/palette").text
        assert "/reviews/T-60" in pal and "/t/T-60" not in pal


def test_sweep_still_covers_review_and_backup_keeps_log(home, cfg, tmp_path):
    seed_review_ticket(home, "T-60", brief=False, phase="qa")
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "T-60" in report.spawned
    assert "T-60" in (store.read_json(disp._spawn_ledger_path(home), {}) or {})
    assert "T-60" in disp.list_keys(home)
    path = backup.create_backup(cfg, 2000)
    with tarfile.open(path) as tf:
        assert any(n.endswith("events/T-60.jsonl") for n in tf.getnames())
