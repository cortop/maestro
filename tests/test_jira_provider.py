"""Jira adapter: import_new/refresh unit behavior + a real dispatch sweep proving
the wiring (mint -> `_new` inbox -> tickets, JiraSynced events, idempotency)."""
from maestro import dispatcher as disp
from maestro import event_log, inbox, providers, snapshot as snap_mod, store
from maestro.providers.jira import JiraTracker, adf_to_text
from maestro.sessions import DryRunSessions


def _adf_para(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


class FakeJiraTransport:
    """The only mock: the external Jira HTTP boundary."""

    def __init__(self, issues, comments=None):
        self.issues = issues  # list of {"key": ..., "fields": {...}}
        self.comments = comments or {}  # key -> list of {"id": ...}
        self.calls = []

    def search_jql(self, jql, fields):
        self.calls.append(("search_jql", jql))
        return self.issues

    def get_issue(self, key, fields):
        self.calls.append(("get_issue", key))
        for issue in self.issues:
            if issue["key"] == key:
                return issue
        return {}

    def get_comments(self, key):
        return self.comments.get(key, [])


def test_adf_to_text_flattens_paragraphs():
    assert adf_to_text(_adf_para("Do the thing")).strip() == "Do the thing"
    assert adf_to_text(None) == ""
    assert adf_to_text({}) == ""


def test_import_new_enqueues_and_dedups(home):
    issue = {"key": "ACME-1", "fields": {"summary": "Fix the widget", "description": _adf_para("Widget is broken")}}
    transport = FakeJiraTransport(issues=[issue])
    tracker = JiraTracker({}, transport=transport)

    count = tracker.import_new(home)
    assert count == 1
    pending = inbox.pending_new(home)
    assert len(pending) == 1
    _idx, entry = pending[0]
    assert entry["key"] == "JIRA-ACME-1"
    assert entry["title"] == "Fix the widget"
    assert entry["args"]["external_source"] == "jira"
    assert entry["args"]["external_id"] == "ACME-1"
    assert "Widget is broken" in entry["args"]["intent"]

    # Mint the queued ticket (as a real dispatch sweep would).
    cfg = _cfg_for(home)
    disp.mint_new_tickets(cfg)
    assert store.spec_path(home, "JIRA-ACME-1").exists()

    # A second import of the same still-open Jira issue must not re-enqueue.
    count2 = tracker.import_new(home)
    assert count2 == 0


def test_refresh_is_idempotent_and_notes_changes(home):
    key = "JIRA-ACME-2"
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated",
                      {"title": "t", "spec_hash": "x", "external_source": "jira", "external_id": "ACME-2"},
                      actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": "implementing"}, actor="r")
    snap_mod.rebuild(home, key)

    issue = {"key": "ACME-2", "fields": {"status": {"name": "In Progress"}, "updated": "2026-01-01T00:00:00.000+0000"}}
    transport = FakeJiraTransport(issues=[issue], comments={"ACME-2": [{"id": "10001"}]})
    tracker = JiraTracker({}, transport=transport)

    appended = tracker.refresh(home, key, "ACME-2")
    assert appended == 1  # first sync: baseline JiraSynced, no Note yet
    events = event_log.read(home, key)
    assert any(e["type"] == "JiraSynced" for e in events)

    # Re-running refresh with unchanged Jira state is a no-op.
    appended_again = tracker.refresh(home, key, "ACME-2")
    assert appended_again == 0

    # Jira-side update (new comment) produces a JiraSynced + a human-visible Note.
    issue["fields"]["updated"] = "2026-01-02T00:00:00.000+0000"
    transport.comments["ACME-2"].append({"id": "10002"})
    appended3 = tracker.refresh(home, key, "ACME-2")
    assert appended3 == 2
    events = event_log.read(home, key)
    notes = [e for e in events if e["type"] == "Note"]
    assert len(notes) == 1
    assert "10002" in notes[0]["payload"]["text"]

    # Idempotent by step_id even if called again with the same (already-seen) state.
    appended4 = tracker.refresh(home, key, "ACME-2")
    assert appended4 == 0


def _cfg_for(home):
    from maestro.config import Config
    return Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3)


def test_dispatch_sweep_imports_then_mints_and_refreshes(home, monkeypatch):
    """Real-app QA: two `dispatch()` sweeps (mint lags import by one sweep, matching
    the existing `_new` inbox model) prove Jira import, ticket minting, and
    tracked-ticket refresh all wire together, with no duplicate work on repeats.
    auto_import is opted in explicitly (T-110's default-off gate is covered in
    tests/test_linear_import.py)."""
    cfg = _cfg_for(home)
    cfg.providers["tracker"] = "jira"
    cfg.provider_config = {"tracker": {"jira": {"sync_interval": 0, "auto_import": True}}}

    issue = {"key": "ACME-9", "fields": {"summary": "Sync me", "description": _adf_para("desc"),
                                          "status": {"name": "To Do"}, "updated": "2026-01-01T00:00:00.000+0000"}}
    transport = FakeJiraTransport(issues=[issue])
    fake_tracker = JiraTracker({"sync_interval": 0}, transport=transport)
    monkeypatch.setattr(providers, "get_trackers", lambda c: {"jira": fake_tracker})

    # Sweep 1: no existing tickets yet -> import queues JIRA-ACME-9 into `_new`.
    report1 = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report1.minted == []
    assert len(inbox.pending_new(home)) == 1

    # Sweep 2: mint drains the queue into a real ticket; sync refreshes it (not done).
    report2 = disp.dispatch(cfg, DryRunSessions(), now=2000)
    assert "JIRA-ACME-9" in report2.minted
    snap = snap_mod.load(home, "JIRA-ACME-9")
    assert snap.external_source == "jira"
    assert snap.external_id == "ACME-9"
    events = event_log.read(home, "JIRA-ACME-9")
    assert any(e["type"] == "JiraSynced" for e in events)

    # Sweep 3: nothing changed on the Jira side or in maestro -> no duplicate import/events.
    n_events_before = len(events)
    disp.dispatch(cfg, DryRunSessions(), now=3000)
    events_after = event_log.read(home, "JIRA-ACME-9")
    assert len(events_after) == n_events_before
    assert len(store.read_jsonl(store.new_inbox_path(home))) == 1  # no new create-request queued


def test_tracker_none_by_default_skips_sync(home, cfg):
    """Default config (tracker="none") leaves sync a byte-for-byte no-op."""
    report = disp.sync_external_sources(cfg, now=1000)
    assert report == {"imported": 0, "refreshed": 0}
    assert not (home / "derived" / ".sync_cursor.json").exists()
