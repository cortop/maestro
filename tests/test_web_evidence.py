"""Web ticket page evidence: AC rows, PR review threads, CI timeline and the Diff tab."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import add_worktree, git, make_origin_and_repo, seed_ticket  # noqa: E402
from maestro import event_log, snapshot as snap_mod, store  # noqa: E402
from maestro.view import evidence as evidence_view  # noqa: E402
from maestro.web.app import create_app  # noqa: E402

AC = "does the thing (check: true)"


@pytest.fixture
def client(home):
    seed_ticket(home, "T-9", "nine", phase="implementing", pr=9)
    store.atomic_write(store.spec_path(home, "T-9"),
                       f"# T-9\n\n## Intent\nx\n\n## Acceptance criteria\n- [ ] {AC}\n")
    h = snap_mod.ac_hash(snap_mod.parse_acs(store.spec_path(home, "T-9").read_text())[0])
    ev = lambda t, p: event_log.append(home, "T-9", t, p, actor="t")  # noqa: E731
    ev("AcVerified", {"ac_hash": h, "evidence": {"what": "ran <i>it</i>", "where": "t.py:1",
                                                  "result": "green"}})
    ev("AcQaVerdict", {"ac_hash": h, "verdict": "pass", "evidence": "<script>qa</script> ok",
                       "axis": "spec"})
    ev("AcCheckCaptured", {"tree_key": "abc:def", "ac_hash": h, "kind": "check",
                           "command": "true", "exit_code": 0, "passed": True})
    ev("ReviewFeedbackReceived", {"comment_id": "c1", "state": "COMMENTED", "author": "rev",
                                  "body": "fix <b>this</b>", "path": "a.py", "line": 3})
    ev("CiObserved", {"state": "failure", "failing_checks": ["lint"],
                      "failure_excerpt": "boom <u>x</u>"})
    ev("CiObserved", {"state": "success", "failing_checks": []})
    snap_mod.rebuild(home, "T-9")
    with TestClient(create_app(home)) as c:
        yield c


def test_ac_row_expands_to_evidence(client):
    html = client.get("/t/T-9/side").text
    assert 'class="ac-evidence"' in html
    assert "ran &lt;i&gt;it&lt;/i&gt;" in html and "t.py:1" in html and "green" in html
    assert "&lt;script&gt;qa&lt;/script&gt; ok" in html
    assert "true</span>" in html and "passed, exit 0" in html and "stale" in html
    assert "<script>qa" not in html and "<i>it</i>" not in html


def test_ac_check_current_when_tree_matches(client, monkeypatch):
    monkeypatch.setattr(evidence_view, "tree_key", lambda home, key: "abc:def")
    assert "· current" in client.get("/t/T-9/side").text


def test_pr_card_lists_review_threads(client):
    html = client.get("/t/T-9/side").text
    assert 'id="pr-threads"' in html and "rev [COMMENTED] a.py:3" in html
    assert "fix &lt;b&gt;this&lt;/b&gt;" in html and "<b>this</b>" not in html


def test_ci_timeline_lists_every_observation(client):
    html = client.get("/t/T-9/side").text
    assert 'id="ci-timeline"' in html
    assert html.count('<span class="mono">20') >= 2
    assert "failing: lint" in html and "boom &lt;u&gt;x&lt;/u&gt;" in html
    assert "v-failure" in html and "v-success" in html


def test_diff_tab_without_worktree(client):
    assert 'hx-get="/t/T-9/diff"' in client.get("/t/T-9").text
    html = client.get("/t/T-9/diff").text
    assert "No worktree for T-9" in html and "diff-text" not in html


def test_diff_tab_shows_diff_and_truncates(client, home, tmp_path, monkeypatch):
    _, repo = make_origin_and_repo(tmp_path)
    wt = add_worktree(repo, home, "T-9", "maestro/T-9")
    (wt / "new.txt").write_text("hello <em>diff</em>\n" * 50)
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "c", cwd=wt)
    html = client.get("/t/T-9/diff").text
    assert 'id="diff-text"' in html and "+++ b/new.txt" in html
    assert "<em>diff</em>" not in html and "&lt;em&gt;diff&lt;/em&gt;" in html
    assert "truncated" not in html
    monkeypatch.setattr(evidence_view, "DIFF_CAP", 100)
    html = client.get("/t/T-9/diff").text
    assert "Diff truncated at 100 characters" in html
