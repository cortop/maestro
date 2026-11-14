"""T-262: the web Spec tab edits spec.md, its ACs, front-matter and runner, and fires post-QA."""
from __future__ import annotations

import json
import subprocess

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import event_log, store  # noqa: E402
from maestro.sessions import DryRunSessions, RoutingSessions  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402


def _spec(home, key="T-1"):
    return store.spec_path(home, key).read_text(encoding="utf-8")


def _fake_claude(suggestions):
    def run(cmd, **kw):
        out = json.dumps({"result": json.dumps(suggestions)})
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    return run


@pytest.fixture
def env(home):
    (home / "config.toml").write_text('[maestro]\npost_qa_skill = "/my-pr-polish"\n', encoding="utf-8")
    seed_ticket(home, "T-1", "editable", phase="implementing", pr=7)
    seed_ticket(home, "T-2", "other", phase="ready")
    seed_ticket(home, "T-3", "no pr", phase="ready")
    sessions = RoutingSessions({"claude": DryRunSessions()})
    app = create_app(home, nudge_on_action=False, sessions=sessions,
                     suggest_run=_fake_claude(["first proposal", "second proposal"]))
    with TestClient(app) as c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        yield c, home, sessions


def _hash(c, key="T-1"):
    html = c.get(f"/t/{key}/spec").text
    return html.split('name="hash" value="')[1].split('"')[0]


def test_editor_saves_atomically_and_refuses_a_stale_hash_keeping_typed_text(env):
    c, home, _ = env
    h = _hash(c)
    r = c.post("/t/T-1/spec/save", data={"hash": h, "text": "# T-1\r\n\r\n## Intent\r\nnew body\r\n"})
    assert r.status_code == 200 and "Spec saved." in r.text
    assert _spec(home) == "# T-1\n\n## Intent\nnew body\n"
    # the editor loaded `h`, then the file changed underneath it
    r = c.post("/t/T-1/spec/save", data={"hash": h, "text": "my precious unsaved words"})
    assert r.status_code == 409 and "changed since this editor loaded" in r.text
    assert "my precious unsaved words" in r.text
    assert _spec(home) == "# T-1\n\n## Intent\nnew body\n"
    assert not list(store.spec_path(home, "T-1").parent.glob("*.tmp*"))


def test_add_ac_appends_a_checkbox_line(env):
    c, home, _ = env
    r = c.post("/t/T-1/spec/ac", data={"text": "  it shows up  "})
    assert r.status_code == 200
    assert _spec(home).rstrip().endswith("- [ ] ok\n- [ ] it shows up")
    r = c.post("/t/T-1/spec/ac", data={"text": "   "})
    assert r.status_code == 409 and "must not be blank" in r.text


def test_priority_and_depends_on_with_inline_refusals(env):
    c, home, _ = env
    r = c.post("/t/T-1/spec/fields", data={"priority": "3", "depends_on": "T-2"})
    assert r.status_code == 200
    spec = _spec(home)
    assert "priority: 3" in spec and "dependsOn: [T-2]" in spec
    for deps, msg in (("T-999", "unknown dependency T-999"), ("T-1", "cannot depend on itself")):
        r = c.post("/t/T-1/spec/fields", data={"priority": "3", "depends_on": deps})
        assert r.status_code == 409 and 'class="action-error"' in r.text and msg in r.text
    # T-2 -> T-1 would close a cycle once T-1 depends on T-2
    r = c.post("/t/T-2/spec/fields", data={"priority": "1", "depends_on": "T-1"})
    assert r.status_code == 409 and "cycle T-2 → T-1 → T-2" in r.text and "dependsOn" not in _spec(home, "T-2")
    r = c.post("/t/T-1/spec/fields", data={"priority": "x", "depends_on": ""})
    assert r.status_code == 409 and "integer" in r.text


def test_runner_set_and_refusal_shown_inline(env):
    c, home, _ = env
    r = c.post("/t/T-2/spec/runner", data={"runner": "opencode", "runner_model": ""})
    assert r.status_code == 200 and "runner: opencode" in _spec(home, "T-2")
    r = c.post("/t/T-2/spec/runner", data={"runner": "", "runner_model": ""})
    assert r.status_code == 409 and 'class="action-error"' in r.text and "required" in r.text


def test_suggest_acs_lists_rows_and_appends_only_ticked(env):
    c, home, _ = env
    r = c.post("/t/T-1/spec/suggest", data={})
    assert r.status_code == 200
    assert "first proposal" in r.text and "second proposal" in r.text
    assert 'name="pick-0"' in r.text and 'name="pick-1"' in r.text
    assert "proposal" not in _spec(home)
    r = c.post("/t/T-1/spec/suggest/add", data={"count": "2", "text-0": "first proposal",
                                                "text-1": "second proposal", "pick-1": "1"})
    assert r.status_code == 200
    spec = _spec(home)
    assert "- [ ] second proposal" in spec and "first proposal" not in spec
    r = c.post("/t/T-1/spec/suggest/add", data={"count": "1", "text-0": "x"})
    assert r.status_code == 409 and "tick at least one" in r.text and "x" in r.text


def test_post_qa_needs_confirm_an_open_pr_and_fires_the_skill(env):
    c, home, sessions = env
    assert "Trigger post-QA" in c.get("/t/T-1/spec").text
    assert "Trigger post-QA" not in c.get("/t/T-3/spec").text
    assert c.post("/t/T-1/spec/post-qa", data={}).status_code == 409
    assert sessions.delegates["claude"].spawned == []
    assert c.post("/t/T-3/spec/post-qa", data={"confirm": "1"}).status_code == 409
    r = c.post("/t/T-1/spec/post-qa", data={"confirm": "1"})
    assert r.status_code == 200 and "/my-pr-polish" in r.text
    assert sessions.delegates["claude"].spawned[0][1] == "/my-pr-polish T-1"
    assert any(e["type"] == "PostQaSkillSpawned" for e in event_log.read(home, "T-1"))


def test_palette_lists_spec_actions_and_post_qa_only_with_a_pr(env):
    c, *_ = env
    with_pr = c.get("/palette", params={"key": "T-1"}).text
    without = c.get("/palette", params={"key": "T-3"}).text
    for label in ("Edit spec of T-1", "Add acceptance criterion to T-1", "Suggest acceptance criteria for T-1",
                  "Set priority or dependencies of T-1", "Set runner of T-1", "Trigger post-QA for T-1"):
        assert label in with_pr
    assert "Edit spec of T-3" in without and "Trigger post-QA" not in without


def test_read_only_renders_no_controls_and_refuses_every_post(home):
    seed_ticket(home, "T-1", "editable", phase="implementing", pr=7)
    app = create_app(home, nudge_on_action=False, read_only=True)
    with TestClient(app) as c:
        html = c.get("/t/T-1/spec").text
        assert "<h2>Intent</h2>" in html and "<form" not in html and "<textarea" not in html
        assert "Edit spec" not in c.get("/palette", params={"key": "T-1"}).text
        c.headers[TOKEN_HEADER] = app.state.guard.token
        for action in ("save", "ac", "fields", "runner", "suggest", "suggest/add", "post-qa"):
            assert c.post(f"/t/T-1/spec/{action}", data={"text": "x", "confirm": "1"}).status_code in (403, 404, 405)
    assert "x" not in _spec(home).splitlines()[-1]


def test_writes_disabled_on_remote_bind_hide_controls(home):
    seed_ticket(home, "T-1", "editable", phase="implementing", pr=7)
    with TestClient(create_app(home, host="0.0.0.0")) as c:
        assert "<form" not in c.get("/t/T-1/spec").text
