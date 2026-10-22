"""T-195: third-party text in the dossier is capped and fenced; skills carry the data rule."""
from __future__ import annotations

import re
from pathlib import Path

from maestro import context, event_log, ops
from maestro.statemachine import Phase

ROOT = Path(__file__).resolve().parent.parent


def _dossier(cfg, key):
    return context.context_path(cfg.home, key).read_text(encoding="utf-8")


def _reach_implementing(cfg, key="T-1"):
    event_log.append(cfg.home, key, "TicketCreated", {"title": "W"}, actor="dispatcher")
    ops.set_phase(cfg, key, Phase.READY, reason="approved: ok")
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="worktree ready")


def test_long_reason_capped(cfg):
    _reach_implementing(cfg)
    big = "x" * (context.MAX_ITEM_CHARS + 500)
    ops.set_phase(cfg, "T-1", Phase.QA, reason=big, force=True)
    text = _dossier(cfg, "T-1")
    assert "x" * (context.MAX_ITEM_CHARS + 1) not in text
    assert "maestro events T-1" in text


def test_reason_fenced(cfg):
    _reach_implementing(cfg)
    reason = "changes requested: <b>hi</b>\n# Big Heading\n```py\ncode\n```\n````"
    ops.set_phase(cfg, "T-1", Phase.QA, reason=reason, force=True)
    text = _dossier(cfg, "T-1")
    assert "`````" in text  # longer than the 4-backtick run inside
    lines = text.split("\n")
    i = lines.index("  # Big Heading")
    assert lines[i - 1].strip() == "changes requested: <b>hi</b>"
    assert lines[i - 2].strip() == "`````"


def test_event_payload_unchanged(cfg):
    _reach_implementing(cfg)
    reason = "review comment: ```\n# H\n```"
    ops.set_phase(cfg, "T-1", Phase.QA, reason=reason, force=True)
    ev = [e for e in event_log.read(cfg.home, "T-1") if e["type"] == "PhaseChanged"][-1]
    assert ev["payload"]["reason"] == reason
    assert ev["payload"]["phase"] == "qa"


def test_every_skill_has_inputs_are_data_rule():
    files = sorted((ROOT / ".claude" / "commands").glob("maestro-reconcile-*.md"))
    assert files
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert re.search(r"third-party text is data", text), f.name
        assert "`allowed_paths`" in text and "credentials, remotes, permissions" in text, f.name
