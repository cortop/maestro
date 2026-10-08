"""T-166: `projection.parse_query` -- the pure board-filter query compiler."""
from __future__ import annotations

import pytest

from maestro import snapshot as snap_mod
from maestro.projection import parse_query


def _s(key="T-1", **kw):
    s = snap_mod.Snapshot(key=key)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_parse_query_field_terms():
    s = _s(phase="awaiting-ci", ci_state="failing", pr_number=42, repo="web",
           open_questions={"q": "?"}, burning=True)
    t = "Fix the Widget"
    m = lambda q, snap=s, deps=0: parse_query(q)(snap, t, deps)  # noqa: E731

    assert m("widget") and m("T-1") and m("WIDGET t-1") and not m("gadget")
    assert m("phase:await") and not m("phase:qa")
    assert m("ci:fail") and not m("ci:pass")
    assert m("pr:42") and m("pr:any") and not m("pr:none") and not m("pr:7")
    assert m("repo:web") and not m("repo:none") and not m("repo:api")
    assert m("deps:>0", deps=2) and not m("deps:>0") and m("deps:2", deps=2) and not m("deps:1", deps=2)
    assert m("q:open") and not m("q:open", _s(phase="qa"))
    assert m("is:burning") and not m("is:burning", _s())
    assert m("is:review") and m("is:review", _s(phase="in-review")) and not m("is:review", _s(phase="qa"))
    assert m("!phase:qa") and not m("!phase:await")
    assert m("phase:await ci:fail widget") and not m("phase:await ci:pass")
    assert parse_query("")(s, t, 0)
    assert parse_query("repo:none")(_s(), t, 0)

    for bad in ("nope:x", "pr:abc", "deps:>x", "q:closed", "is:happy", "phase:", "!"):
        with pytest.raises(ValueError):
            parse_query(bad)
