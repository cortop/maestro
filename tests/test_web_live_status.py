"""T-226: the web board row renders the live Doing text, HTML-escaped."""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_phase  # noqa: E402
from maestro import claims, event_log, snapshot as snap_mod  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


def test_web_row_shows_escaped_doing(home, tmp_path):
    seed_phase(home, "T-1")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": "implementing", "reason": "worktree ready"}, actor="r")
    snap_mod.rebuild(home, "T-1")
    log = tmp_path / "a.stream.jsonl"
    rec = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Step: Fixing <script>alert(1)</script>"}]}}
    log.write_text(json.dumps(rec) + "\n")
    claims.write_claim(home, "T-1", os.getpid(), "t", log_path=str(log))
    with TestClient(create_app(home)) as c:
        html = c.get("/board/rows?filter=all").text
    assert "Fixing &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert" not in html
