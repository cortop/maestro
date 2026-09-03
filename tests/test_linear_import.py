"""T-103: Linear ticket-ID first-class field + import-by-URL/identifier.

The only mock anywhere in this file is `FakeLinearTransport` -- the external
HTTP boundary `LinearTracker` talks to. Everything else (parsing, minting,
dedup, the real `maestro import-linear` CLI verb) is exercised for real.
"""
from maestro import event_log, ops, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.providers.linear import LinearTracker, parse_identifier


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
