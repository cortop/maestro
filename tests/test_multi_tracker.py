"""Multi-tracker registry (AD-5): more than one tracker can be configured at once,
each ticket refreshes against the ONE tracker matching its own `external_source`,
and each tracker advances its own sync cursor independently."""
from maestro import dispatcher as disp
from maestro import event_log, inbox, providers, snapshot as snap_mod, store
from maestro.config import Config
from maestro.providers.jira import JiraTracker
from maestro.providers.linear import LinearTracker
from maestro.sessions import DryRunSessions


def _adf_para(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


class FakeJiraTransport:
    def __init__(self, issues):
        self.issues = issues

    def search_jql(self, jql, fields):
        return self.issues

    def get_issue(self, key, fields):
        for issue in self.issues:
            if issue["key"] == key:
                return issue
        return {}

    def get_comments(self, key):
        return []


class FakeLinearTransport:
    def __init__(self, issues):
        self.issues = issues

    def search_issues(self, filter):
        return self.issues

    def get_issue(self, identifier):
        for issue in self.issues:
            if issue["identifier"] == identifier:
                return issue
        return {}

    def get_comments(self, identifier):
        return []


def _cfg_for(home):
    return Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3)


def test_get_trackers_resolves_a_keyed_set(home):
    """AC1: `[providers] tracker` can declare more than one name, and each resolves
    to its own adapter instance keyed by that name."""
    cfg = _cfg_for(home)
    cfg.providers["tracker"] = ["jira", "linear"]
    trackers = providers.get_trackers(cfg)
    assert set(trackers) == {"jira", "linear"}
    assert isinstance(trackers["jira"], JiraTracker)
    assert isinstance(trackers["linear"], LinearTracker)

    # Back-compat: a single string still resolves to a one-entry set.
    cfg.providers["tracker"] = "jira"
    assert set(providers.get_trackers(cfg)) == {"jira"}

    # Default "none" resolves to nothing.
    cfg.providers["tracker"] = "none"
    assert providers.get_trackers(cfg) == {}


def test_dispatch_sweep_imports_and_refreshes_both_trackers(home, monkeypatch):
    """AC2 + AC4: a real dispatcher sweep, with two fake trackers configured at
    once, imports both sets of tickets and refreshes each one against its OWN
    tracker (never the other one's) via a per-source cursor. auto_import is
    opted in explicitly here (T-110's default-off gate lives in
    test_linear_import.py) so this test keeps exercising the import half."""
    cfg = _cfg_for(home)
    cfg.providers["tracker"] = ["jira", "linear"]
    cfg.provider_config = {
        "tracker": {
            "jira": {"sync_interval": 0, "auto_import": True},
            "linear": {"sync_interval": 0, "auto_import": True},
        }
    }

    jira_issue = {"key": "ACME-1", "fields": {
        "summary": "Fix the widget", "description": _adf_para("desc"),
        "status": {"name": "To Do"}, "updated": "2026-01-01T00:00:00.000+0000"}}
    linear_issue = {"identifier": "ENG-1", "title": "Ship the gadget",
                     "description": "desc", "state": {"name": "Todo"},
                     "updatedAt": "2026-01-01T00:00:00.000Z"}

    jira_tracker = JiraTracker({"sync_interval": 0}, transport=FakeJiraTransport([jira_issue]))
    linear_tracker = LinearTracker({"sync_interval": 0}, transport=FakeLinearTransport([linear_issue]))
    monkeypatch.setattr(
        providers, "get_trackers",
        lambda c: {"jira": jira_tracker, "linear": linear_tracker})

    # Sweep 1: import queues both create-requests into `_new`.
    report1 = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert report1.minted == []
    pending = {e["key"] for _, e in inbox.pending_new(home)}
    assert pending == {"JIRA-ACME-1", "LINEAR-ENG-1"}

    # Sweep 2: mint drains the queue; sync refreshes each against its own tracker.
    report2 = disp.dispatch(cfg, DryRunSessions(), now=2000)
    assert set(report2.minted) == {"JIRA-ACME-1", "LINEAR-ENG-1"}

    jira_snap = snap_mod.load(home, "JIRA-ACME-1")
    assert jira_snap.external_source == "jira"
    assert any(e["type"] == "JiraSynced" for e in event_log.read(home, "JIRA-ACME-1"))

    linear_snap = snap_mod.load(home, "LINEAR-ENG-1")
    assert linear_snap.external_source == "linear"
    assert any(e["type"] == "LinearSynced" for e in event_log.read(home, "LINEAR-ENG-1"))
    # The Linear ticket must NOT have been touched by the Jira tracker or vice versa.
    assert not any(e["type"] == "LinearSynced" for e in event_log.read(home, "JIRA-ACME-1"))
    assert not any(e["type"] == "JiraSynced" for e in event_log.read(home, "LINEAR-ENG-1"))

    # Sweep 3: nothing changed -> no duplicate imports or sync events.
    before_jira = len(event_log.read(home, "JIRA-ACME-1"))
    before_linear = len(event_log.read(home, "LINEAR-ENG-1"))
    disp.dispatch(cfg, DryRunSessions(), now=3000)
    assert len(event_log.read(home, "JIRA-ACME-1")) == before_jira
    assert len(event_log.read(home, "LINEAR-ENG-1")) == before_linear
    assert len(store.read_jsonl(store.new_inbox_path(home))) == 2


def test_per_source_cursor_gates_each_tracker_independently(home, monkeypatch):
    """AC2: the sync cursor is keyed per tracker name, so a due Jira tracker still
    imports/refreshes even while Linear's `sync_interval` hasn't elapsed yet."""
    cfg = _cfg_for(home)
    cfg.providers["tracker"] = ["jira", "linear"]
    cfg.provider_config = {
        "tracker": {
            "jira": {"sync_interval": 0, "auto_import": True},
            "linear": {"sync_interval": 999999, "auto_import": True},
        }
    }

    jira_issue = {"key": "ACME-2", "fields": {
        "summary": "t", "description": _adf_para("d"),
        "status": {"name": "To Do"}, "updated": "2026-01-01T00:00:00.000+0000"}}
    linear_issue = {"identifier": "ENG-2", "title": "t2", "description": "d2",
                     "state": {"name": "Todo"}, "updatedAt": "2026-01-01T00:00:00.000Z"}

    jira_tracker = JiraTracker({"sync_interval": 0}, transport=FakeJiraTransport([jira_issue]))
    linear_tracker = LinearTracker({"sync_interval": 999999}, transport=FakeLinearTransport([linear_issue]))
    monkeypatch.setattr(
        providers, "get_trackers",
        lambda c: {"jira": jira_tracker, "linear": linear_tracker})

    report = disp.sync_external_sources(cfg, now=1000)
    assert report["imported"] == 1  # only Jira's issue -- Linear isn't due yet
    pending = {e["key"] for _, e in inbox.pending_new(home)}
    assert pending == {"JIRA-ACME-2"}
