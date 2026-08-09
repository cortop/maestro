"""A ticket with no TicketCreated event still shows its spec's title.

`dispatcher.list_keys` discovers a ticket from a bare `tickets/<KEY>/` directory,
and `mint_new_tickets` deliberately declines to append a second `TicketCreated`
to a key that already has events (it would clobber the folded phase back to
triaging). Both are supported states — and in both the snapshot folds to
`title = None`, which every human-facing surface used to render blank even though
the spec's own H1 carries the title.
"""
from __future__ import annotations

import json

from maestro import cli, event_log, notify, projection, snapshot as snap_mod, store
from maestro.config import Config


def _titleless_ticket(home, key="X-1", title="A torn tail swallows the next append",
                      phase="awaiting-human"):
    """A ticket whose log has events but NO TicketCreated -- exactly what a
    directory-discovered or already-advanced key looks like."""
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}: {title}\n\napproval_tier: 1\n\n## Intent\nbody\n")
    event_log.append(home, key, "SpecObserved", {"spec_hash": "abc"}, actor="r")
    event_log.append(home, key, "PhaseChanged", {"phase": phase, "reason": ""}, actor="r")
    return snap_mod.rebuild(home, key)


# --------------------------------------------------------------------------- #
# the pure parser                                                              #
# --------------------------------------------------------------------------- #

def test_parse_title_strips_the_key_prefix():
    assert snap_mod.parse_title("# X-1: Real title\n\nbody", "X-1") == "Real title"


def test_parse_title_without_a_key_prefix():
    assert snap_mod.parse_title("# Just a title\n\nbody", "X-1") == "Just a title"
    assert snap_mod.parse_title("# X-1: Real title\n") == "X-1: Real title"


def test_parse_title_is_total_on_specs_with_no_heading():
    assert snap_mod.parse_title("no heading here\n", "X-1") is None
    assert snap_mod.parse_title("", "X-1") is None
    assert snap_mod.parse_title("# \n", "X-1") is None
    # a '#' that is not a heading must not be mistaken for one
    assert snap_mod.parse_title("#nospace\n", "X-1") is None


def test_parse_title_takes_the_first_heading_only():
    assert snap_mod.parse_title("# First\n\n# Second\n", None) == "First"


# --------------------------------------------------------------------------- #
# the shared fallback                                                          #
# --------------------------------------------------------------------------- #

def test_display_title_prefers_the_folded_title(home):
    """A normally-minted ticket is unaffected -- the log stays authoritative."""
    store.atomic_write(store.spec_path(home, "X-2"), "# X-2: spec heading\n")
    event_log.append(home, "X-2", "TicketCreated",
                     {"title": "folded title", "source": "t", "spec_hash": "x"}, actor="d")
    snap = snap_mod.rebuild(home, "X-2")
    assert snap.title == "folded title"
    assert snap_mod.display_title(home, snap) == "folded title"


def test_display_title_falls_back_to_the_spec_heading(home):
    snap = _titleless_ticket(home)
    assert snap.title is None
    assert snap_mod.display_title(home, snap) == "A torn tail swallows the next append"


def test_display_title_is_total_when_the_spec_is_missing(home):
    event_log.append(home, "X-3", "SpecObserved", {"spec_hash": "abc"}, actor="r")
    snap = snap_mod.rebuild(home, "X-3")
    assert snap_mod.display_title(home, snap) == ""


# --------------------------------------------------------------------------- #
# the real surfaces                                                            #
# --------------------------------------------------------------------------- #

def test_workstate_dashboard_shows_the_spec_title(home):
    _titleless_ticket(home, "X-1", "A torn tail swallows the next append")
    projection.write(home)
    workstate = (home / "derived" / "WORKSTATE.md").read_text(encoding="utf-8")
    row = next(l for l in workstate.splitlines() if l.startswith("| X-1 "))
    assert "A torn tail swallows the next append" in row


def test_project_cli_verb_renders_the_spec_title(home, capsys):
    """The real CLI, end to end -- `maestro project` regenerates the dashboards."""
    _titleless_ticket(home, "X-1", "ops.compact can lose archived events")
    assert cli.main(["--home", str(home), "project"]) == 0
    workstate = (home / "derived" / "WORKSTATE.md").read_text(encoding="utf-8")
    assert "ops.compact can lose archived events" in workstate


def test_ticket_rows_carry_the_spec_title(home):
    """The tuple the TUI's DataTable is built from (title is column index 2)."""
    _titleless_ticket(home, "X-1", "the spec heading")
    rows = projection.ticket_rows(home)
    row = next(r for r in rows if r[0] == "X-1")
    assert row[2] == "the spec heading"


def test_notify_webhook_payload_carries_the_spec_title(home, monkeypatch):
    """The outbound payload had the same blank-title bug (notify.py)."""
    _titleless_ticket(home, "X-1", "an unset daily spend ceiling", phase="awaiting-human")
    posted: list[dict] = []
    monkeypatch.setattr(notify, "_post_webhook", lambda url, payload: posted.append(payload))
    cfg = Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3)
    cfg.webhook_urls = ["https://example.invalid/hook"]
    cfg.notify_command = None
    assert notify.maybe_notify(cfg, now=1_000) == ["X-1"]
    assert posted, "expected one webhook payload for the awaiting-human ticket"
    assert posted[0]["title"] == "an unset daily spend ceiling"


def test_title_fallback_survives_a_snapshot_rebuild(home):
    """The fallback is a render-time read, so it must not be persisted into the
    snapshot -- the log stays the sole source of truth for `title`."""
    _titleless_ticket(home, "X-1", "not folded into the snapshot")
    projection.write(home)
    persisted = json.loads((home / "derived" / "snapshots" / "X-1.json").read_text())
    assert persisted["title"] is None
