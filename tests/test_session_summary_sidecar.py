"""`prune-logs` leaves a digest sidecar that rounds and `logs --summary` keep showing."""
from __future__ import annotations

import io
import json
import os
import sys
import time

from conftest import seed_ticket

from maestro import cli, event_log, sessions, store
from maestro.view import rounds as rounds_view

KEY = "T-950"


def _run(home, args):
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["--home", str(home), *args])
    finally:
        sys.stdout = old
    return rc, buf.getvalue()


def _log(home, epoch):
    d = home / "agent-logs" / KEY
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"reconcile-{KEY}-{epoch}.stream.jsonl"
    lines = [{"type": "system", "subtype": "init", "session_id": "s"}]
    for i, cmd in enumerate(("ls", "make test")):
        lines.append({"type": "assistant", "message": {"id": f"m{i}", "role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Bash",
             "input": {"command": cmd, "description": cmd}}]}})
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    os.utime(p, (float(epoch) + 30, float(epoch) + 30))
    return p


def _setup(home):
    cli.main(["--home", str(home), "init"])
    (home / "config.toml").write_text(
        "[maestro]\nsession_log_retention_days = 5\nsession_log_max_per_ticket = 0\n", encoding="utf-8")
    seed_ticket(home, KEY, "Synthetic", phase="implementing")
    epoch = time.time() - 10 * 86400
    return _log(home, f"{epoch:.1f}"), f"{epoch:.1f}"


def test_prune_writes_sidecar_before_delete_and_never_lists_or_deletes_it(home):
    log, epoch = _setup(home)
    n_events = len(event_log.read(home, KEY))
    rc, out = _run(home, ["prune-logs", KEY])
    assert rc == 0 and json.loads(out)["pruned_logs"] == 1
    assert not log.exists()
    side = log.parent / f"reconcile-{KEY}-{epoch}.summary.json"
    data = json.loads(side.read_text())
    assert data["digest"]["calls"] == 2 and data["session"]["format"] == "stream-json"
    assert len(event_log.read(home, KEY)) == n_events  # no per-session events
    assert sessions.list_sessions(home, KEY) == []
    rc, out = _run(home, ["prune-logs", KEY])
    assert json.loads(out)["pruned_logs"] == 0 and side.exists()
    _run(home, ["prune-logs", "--all"])
    assert side.exists()


def test_summary_and_rounds_show_sidecar_after_prune(home):
    log, epoch = _setup(home)
    sid = f"reconcile-{KEY}-{epoch}"
    _, before = _run(home, ["logs", KEY, "--summary", "--session", sid])
    _run(home, ["prune-logs", KEY])
    rc, after = _run(home, ["logs", KEY, "--summary", "--session", sid])
    assert rc == 0 and "2 calls" in after and "2 calls" in before
    rc, js = _run(home, ["logs", KEY, "--summary", "--session", sid, "--json"])
    assert json.loads(js)["calls"] == 2
    assert [s["session_id"] for s in sessions.list_pruned(home, KEY)] == [sid]
    assert rounds_view.build(home, KEY).key == KEY
