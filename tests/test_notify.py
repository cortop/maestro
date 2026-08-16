"""Outbound notify tick: fires exactly once per new entry into a watched phase.

Every test drives the REAL surface: real `ops`/CLI verbs plus a real dispatcher
sweep over a temp home — never a mocked `notify` module.
"""
from maestro import dispatcher as disp, event_log, inbox, notify, ops, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _create(cfg, key):
    store.atomic_write(store.spec_path(cfg.home, key),
                        f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(cfg.home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(cfg.home, key)}, actor="d")
    snap_mod.rebuild(cfg.home, key)


def test_notify_fires_once_then_again_on_a_fresh_entry(cfg, tmp_path):
    log_path = tmp_path / "notify.log"
    cfg.notify_command = f'printf "%s %s %s\\n" "$KEY" "$PHASE" "$QUESTION" >> {log_path}'
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "Can I pick this up?", qid="q1")
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value

    disp.dispatch(cfg, DryRunSessions(), now=1_000)
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0] == "T-1 awaiting-human Can I pick this up?"

    # Same phase, no new entry -> no second line.
    disp.dispatch(cfg, DryRunSessions(), now=1_100)
    assert log_path.read_text().splitlines() == lines

    # Leave and re-enter awaiting-human (a second question) -> fires again.
    inbox.append_command(cfg.home, "T-1", "ans", {"text": "yes", "qid": "q1"})
    ops.fold_inbox(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.READY, reason="approved")
    inbox.ack(cfg.home, "T-1")
    disp.dispatch(cfg, DryRunSessions(), now=1_150)  # sweep observes the departure
    ops.ask(cfg, "T-1", "Which alternative?", qid="q2")
    disp.dispatch(cfg, DryRunSessions(), now=1_200)
    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    assert lines[1] == "T-1 awaiting-human Which alternative?"


def test_notify_fires_on_degraded_and_done(cfg, tmp_path):
    log_path = tmp_path / "notify.log"
    cfg.notify_command = f'printf "%s %s\\n" "$KEY" "$PHASE" >> {log_path}'

    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING)
    ops.fail(cfg, "T-1", "boom")
    ops.fail(cfg, "T-1", "boom")
    ops.fail(cfg, "T-1", "boom")  # 3rd failure hits max_failures=3 -> DEGRADED
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.DEGRADED.value

    _create(cfg, "T-2")
    ops.finalize(cfg, "T-2")
    assert snap_mod.load(cfg.home, "T-2").phase == Phase.DONE.value

    disp.dispatch(cfg, DryRunSessions(), now=1_000)
    lines = sorted(log_path.read_text().splitlines())
    assert lines == ["T-1 degraded", "T-2 done"]


def test_no_op_when_unconfigured(cfg):
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "ok?", qid="q1")
    assert notify.maybe_notify(cfg, now=1_000) == []
    assert not (cfg.home / "derived" / ".notify_cursor.json").exists()


def test_webhook_posts_json_payload(cfg, monkeypatch):
    posted = []

    class _Resp:
        def close(self):
            pass

    def fake_urlopen(req, timeout):
        import json as _json
        posted.append(_json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr("maestro.notify.urllib.request.urlopen", fake_urlopen)
    cfg.webhook_urls = ["https://example.invalid/hook"]
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "ok?", qid="q1")

    fired = notify.maybe_notify(cfg, now=1_000)
    assert fired == ["T-1"]
    assert len(posted) == 1
    assert posted[0]["key"] == "T-1"
    assert posted[0]["phase"] == "awaiting-human"
    assert posted[0]["question"] == "ok?"


def test_broken_command_and_unreachable_webhook_never_abort_the_sweep(cfg):
    cfg.notify_command = "this-binary-does-not-exist-anywhere --flag"
    cfg.webhook_urls = ["http://127.0.0.1:1/unreachable"]  # connection refused
    _create(cfg, "T-1")
    ops.ask(cfg, "T-1", "ok?", qid="q1")

    # A second, independently-due ticket (freshly triaging) must still spawn.
    _create(cfg, "T-2")

    report = disp.dispatch(cfg, DryRunSessions(), now=1_000)
    assert snap_mod.load(cfg.home, "T-1").phase == Phase.AWAITING_HUMAN.value
    assert "T-2" in report.spawned
