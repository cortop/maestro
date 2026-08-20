"""T-98 AC6: `maestro doctor`'s `health.check_ac_annotation_parse` -- the
backstop for an AC line that LOOKS like a T-79 `(test: ...)`/`(check: ...)`
annotation but `snapshot.parse_ac_annotation` declines to parse it (e.g. an
unbalanced paren in a `check:` command, or a stray second trailing
parenthetical) -- must never silently degrade to the unenforced prose tier
with zero feedback.
"""
from __future__ import annotations

import io
import json
import sys

from maestro import cli as cli_mod, event_log, health, snapshot as snap_mod, store
from maestro import dispatcher as disp
from maestro.statemachine import Phase

from test_missing_acs import _seed

MALFORMED_CHECK_AC = "- [ ] widget adds (check: bzl test --test_filter='^(TestA')"
SECOND_PARENTHETICAL_AC = "- [ ] widget builds (test: tests/test_widget.py) (see #123)"
REAL_ANNOTATED_AC = "- [ ] widget builds (test: tests/test_widget.py)"
PLAIN_AC = "- [ ] widget builds"


def test_check_ac_annotation_parse_flags_a_malformed_check_body(home, cfg):
    _seed(home, "BAD-1", phase=Phase.READY, ac_body=MALFORMED_CHECK_AC)

    result = health.check_ac_annotation_parse(cfg, 1000)

    assert result["status"] == "warn"
    assert [f["key"] for f in result["flagged"]] == ["BAD-1"]
    assert "bzl test" in result["flagged"][0]["line"]


def test_check_ac_annotation_parse_flags_a_swallowed_second_parenthetical(home, cfg):
    _seed(home, "BAD-1", phase=Phase.READY, ac_body=SECOND_PARENTHETICAL_AC)

    result = health.check_ac_annotation_parse(cfg, 1000)

    assert result["status"] == "warn"
    assert [f["key"] for f in result["flagged"]] == ["BAD-1"]


def test_check_ac_annotation_parse_ok_on_a_correctly_parsing_annotation(home, cfg):
    _seed(home, "OK-1", phase=Phase.READY, ac_body=REAL_ANNOTATED_AC)
    result = health.check_ac_annotation_parse(cfg, 1000)
    assert result["status"] == "ok"
    assert result["flagged"] == []


def test_check_ac_annotation_parse_ok_on_a_plain_unannotated_ac(home, cfg):
    # No "(test:"/"(check:" substring at all -- never flagged.
    _seed(home, "OK-1", phase=Phase.READY, ac_body=PLAIN_AC)
    result = health.check_ac_annotation_parse(cfg, 1000)
    assert result["status"] == "ok"
    assert result["flagged"] == []


def test_check_ac_annotation_parse_skips_terminal_tickets(home, cfg):
    _seed(home, "DONE-1", phase=Phase.READY, ac_body=MALFORMED_CHECK_AC)
    event_log.append(home, "DONE-1", "Finalized", {}, actor="r")
    snap_mod.rebuild(home, "DONE-1")

    result = health.check_ac_annotation_parse(cfg, 1000)

    assert result["status"] == "ok"
    assert result["flagged"] == []


def test_check_ac_annotation_parse_registered_in_the_doctor_check_registry(home, cfg):
    """Real CLI, matching the project's own QA convention."""
    _seed(home, "BAD-1", phase=Phase.READY, ac_body=MALFORMED_CHECK_AC)
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        cli_mod.main(["--home", str(home), "doctor"])
    finally:
        sys.stdout = old
    rpt = json.loads(buf.getvalue())
    names = [c["name"] for c in rpt["checks"]]
    assert "ac_annotation_parse" in names
    check = next(c for c in rpt["checks"] if c["name"] == "ac_annotation_parse")
    assert check["status"] == "warn"
    assert any(f["key"] == "BAD-1" for f in check["flagged"])
