"""T-103: Linear ticket-ID first-class field + import-by-URL/identifier.

The only mock anywhere in this file is `FakeLinearTransport` -- the external
HTTP boundary `LinearTracker` talks to. Everything else (parsing, minting,
dedup, the real `maestro import-linear` CLI verb) is exercised for real.
"""
from maestro import dispatcher as disp
from maestro import event_log, inbox, ops, providers, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.config import Config
from maestro.providers.linear import LinearTracker, parse_identifier
from maestro.sessions import DryRunSessions


class FakeLinearTransport:
    """The only mock: the external Linear HTTP boundary."""

    def __init__(self, issues):
        self.issues = {i["identifier"]: i for i in issues}
        self.calls = []

    def search_issues(self, filter):
        self.calls.append(("search_issues", filter))
        return list(self.issues.values())

    def get_issue(self, identifier):
        self.calls.append(("get_issue", identifier))
        return self.issues.get(identifier, {})

    def get_comments(self, identifier):
        return []


# --- parse_identifier ---------------------------------------------------------

def test_parse_identifier_accepts_bare_id():
    assert parse_identifier("ENG-123") == "ENG-123"
    assert parse_identifier(" eng-123 ") == "ENG-123"  # trimmed + uppercased


def test_parse_identifier_accepts_issue_url():
    assert parse_identifier("https://linear.app/acme/issue/ENG-123/fix-the-thing") == "ENG-123"


def test_parse_identifier_accepts_url_with_no_trailing_slug():
    assert parse_identifier("https://linear.app/acme/issue/ENG-123") == "ENG-123"
    assert parse_identifier("https://linear.app/acme/issue/ENG-123/") == "ENG-123"


def test_parse_identifier_accepts_http_and_lowercase_id_in_url():
    assert parse_identifier("http://linear.app/acme/issue/eng-123/slug") == "ENG-123"


def test_parse_identifier_rejects_malformed_input():
    for bad in ("", "not an id", "https://example.com/issue/ENG-123",
                "https://linear.app/acme/ENG-123", "ENG"):
        try:
            parse_identifier(bad)
            assert False, f"expected MaestroError for {bad!r}"
        except store.MaestroError:
            pass


# --- ops.import_linear ---------------------------------------------------------

def _issue(identifier="ENG-42", title="Fix the widget", description="The widget is broken"):
    return {"identifier": identifier, "title": title, "description": description,
            "state": {"name": "Todo"}}


def test_import_linear_mints_ticket_from_bare_id(cfg):
    transport = FakeLinearTransport([_issue()])
    tracker = LinearTracker({}, transport=transport)

    result = ops.import_linear(cfg, "ENG-42", tracker=tracker)
    assert result == {"key": "LINEAR-ENG-42", "minted": True}

    assert store.spec_path(cfg.home, "LINEAR-ENG-42").exists()
    snap = snap_mod.load(cfg.home, "LINEAR-ENG-42")
    assert snap.external_source == "linear"
    assert snap.external_id == "ENG-42"
    assert "widget is broken" in store.spec_path(cfg.home, "LINEAR-ENG-42").read_text().lower()


def test_import_linear_mints_ticket_from_url(cfg):
    transport = FakeLinearTransport([_issue()])
    tracker = LinearTracker({}, transport=transport)

    result = ops.import_linear(
        cfg, "https://linear.app/acme/issue/ENG-42/fix-the-widget", tracker=tracker)
    assert result == {"key": "LINEAR-ENG-42", "minted": True}


def test_import_linear_falls_back_to_title_when_no_description(cfg):
    transport = FakeLinearTransport([_issue(description=None)])
    tracker = LinearTracker({}, transport=transport)

    ops.import_linear(cfg, "ENG-42", tracker=tracker)
    spec = store.spec_path(cfg.home, "LINEAR-ENG-42").read_text()
    assert "Fix the widget" in spec


def test_import_linear_duplicate_is_clean_noop_and_never_hits_network(cfg):
    transport = FakeLinearTransport([_issue()])
    tracker = LinearTracker({}, transport=transport)

    first = ops.import_linear(cfg, "ENG-42", tracker=tracker)
    assert first["minted"] is True
    calls_after_first = len(transport.calls)

    second = ops.import_linear(cfg, "ENG-42", tracker=tracker)
    assert second == {"key": "LINEAR-ENG-42", "minted": False}
    # No new network call -- the dedup check short-circuits before `view()`.
    assert len(transport.calls) == calls_after_first


def test_import_linear_malformed_input_raises_maestro_error(cfg):
    try:
        ops.import_linear(cfg, "not a linear thing")
        assert False, "expected MaestroError"
    except store.MaestroError:
        pass


def test_import_linear_issue_not_found_raises_maestro_error(cfg):
    transport = FakeLinearTransport([])  # empty -- ENG-42 does not exist
    tracker = LinearTracker({}, transport=transport)
    try:
        ops.import_linear(cfg, "ENG-42", tracker=tracker)
        assert False, "expected MaestroError"
    except store.MaestroError:
        pass
    assert event_log.last_seq(cfg.home, "LINEAR-ENG-42") == 0


# --- the real `maestro import-linear` CLI verb ---------------------------------

def test_cli_import_linear_mints_ticket(home, monkeypatch):
    from maestro.providers import linear as linear_mod

    monkeypatch.setattr(
        linear_mod.LinearTracker, "_transport_or_build",
        lambda self: FakeLinearTransport([_issue()]))

    rc = cli_main(["--home", str(home), "import-linear", "ENG-42"])
    assert rc == 0
    assert store.spec_path(home, "LINEAR-ENG-42").exists()


def test_cli_import_linear_malformed_input_errors_cleanly(home, capsys):
    rc = cli_main(["--home", str(home), "import-linear", "definitely not linear"])
    assert rc == 2  # main()'s MaestroError catch, not a raw traceback
    err = capsys.readouterr().err
    assert "error:" in err


# --- T-110: auto-import is opt-in (default off); explicit `sync-tracker` verb --------

def test_dispatch_sweep_does_not_auto_import_by_default(cfg, monkeypatch):
    """AC1: a real dispatcher sweep, over a home with a Linear tracker
    configured (fake transport) and no `auto_import` override, enqueues no
    new Linear issues into the `_new` inbox on its own."""
    cfg.providers["tracker"] = "linear"
    cfg.provider_config = {"tracker": {"linear": {"sync_interval": 0}}}
    tracker = LinearTracker({"sync_interval": 0}, transport=FakeLinearTransport([_issue()]))
    monkeypatch.setattr(providers, "get_trackers", lambda c: {"linear": tracker})

    report = disp.sync_external_sources(cfg, now=1000)
    assert report["imported"] == 0
    assert inbox.pending_new(cfg.home) == []

    # A full dispatcher sweep (not just the tick function directly) agrees.
    disp.dispatch(cfg, DryRunSessions(), now=2000)
    assert inbox.pending_new(cfg.home) == []


def test_dispatch_sweep_still_refreshes_already_imported_ticket(cfg, monkeypatch):
    """AC2: the same sweep still refreshes an already-imported, not-done
    Linear ticket -- a LinearSynced event is appended when `updatedAt`
    changed -- even though auto_import is off."""
    cfg.providers["tracker"] = "linear"
    cfg.provider_config = {"tracker": {"linear": {"sync_interval": 0}}}
    issue = _issue()
    issue["updatedAt"] = "2026-01-01T00:00:00.000Z"
    mint_tracker = LinearTracker({}, transport=FakeLinearTransport([issue]))
    ops.import_linear(cfg, "ENG-42", tracker=mint_tracker)
    assert snap_mod.load(cfg.home, "LINEAR-ENG-42").external_id == "ENG-42"

    refresh_tracker = LinearTracker({"sync_interval": 0}, transport=FakeLinearTransport([issue]))
    monkeypatch.setattr(providers, "get_trackers", lambda c: {"linear": refresh_tracker})

    report = disp.sync_external_sources(cfg, now=1000)
    assert report["imported"] == 0  # auto_import still off
    assert report["refreshed"] == 1
    assert any(e["type"] == "LinearSynced" for e in event_log.read(cfg.home, "LINEAR-ENG-42"))


def test_cli_sync_tracker_imports_on_demand_and_is_idempotent(home, monkeypatch, capsys):
    """AC3: `maestro sync-tracker` runs the bulk import on demand, enqueues
    the matching issues, exits 0, and reports the count; running it twice is
    idempotent (import_new's own dedup guard holds once the issues are
    minted)."""
    tracker = LinearTracker({}, transport=FakeLinearTransport(
        [_issue(), _issue(identifier="ENG-43", title="Other widget")]))
    monkeypatch.setattr(providers, "get_trackers", lambda c: {"linear": tracker})

    rc = cli_main(["--home", str(home), "sync-tracker", "--name", "linear"])
    assert rc == 0
    out = capsys.readouterr()
    assert '"total": 2' in out.out
    assert "imported 2 issue(s)" in out.err
    pending = {e["key"] for _, e in inbox.pending_new(home)}
    assert pending == {"LINEAR-ENG-42", "LINEAR-ENG-43"}

    # Drain _new into real tickets, exactly like a dispatcher sweep would.
    cfg = Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3)
    disp.mint_new_tickets(cfg)

    rc = cli_main(["--home", str(home), "sync-tracker", "--name", "linear"])
    assert rc == 0
    out = capsys.readouterr()
    assert '"total": 0' in out.out  # both already minted -- dedup guard holds
    assert inbox.pending_new(home) == []


def test_cli_sync_tracker_unknown_name_errors_cleanly(home, monkeypatch):
    monkeypatch.setattr(providers, "get_trackers", lambda c: {"linear": LinearTracker({})})
    rc = cli_main(["--home", str(home), "sync-tracker", "--name", "jira"])
    assert rc == 2  # main()'s MaestroError catch, not a raw traceback


def test_import_linear_single_issue_unaffected_by_auto_import_default(cfg):
    """AC4: `maestro import-linear <url-or-id>` still mints a single ticket
    exactly as before -- it never goes through the auto_import gate at all."""
    transport = FakeLinearTransport([_issue()])
    tracker = LinearTracker({}, transport=transport)

    result = ops.import_linear(cfg, "ENG-42", tracker=tracker)
    assert result == {"key": "LINEAR-ENG-42", "minted": True}
    assert store.spec_path(cfg.home, "LINEAR-ENG-42").exists()
