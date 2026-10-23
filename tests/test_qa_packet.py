"""T-208: QA judges from its own packet -- no `snapshot` grant, extra brief fields,
and a persisted full packet behind a bounded CLI index."""
import json
from pathlib import Path

from maestro import cli, dispatcher, ops

from test_qa_brief import _bind

SKILL = Path(__file__).resolve().parent.parent / ".claude" / "commands" / "maestro-reconcile-qa.md"


def test_qa_has_no_snapshot_grant():
    assert "snapshot" not in dispatcher._PHASE_VERB_GRANT_BY_SUFFIX["qa"]
    assert "maestro snapshot" not in SKILL.read_text(encoding="utf-8")


def test_qa_brief_fields(tmp_path, home):
    cfg, _repo = _bind(tmp_path, home, "T-1")
    brief = ops.qa_brief(cfg, "T-1")
    assert isinstance(brief["phase"], str)
    assert "pr_number" in brief and brief["pr_number"] is None
    assert brief["inbox_pending"] == 0


def test_qa_reads_full_packet(tmp_path, home, capsys):
    cfg, repo = _bind(tmp_path, home, "T-1")
    (repo / "big.txt").write_text("".join(f"line {i}\n" for i in range(2000)), encoding="utf-8")
    rc = cli.main(["--home", str(home), "qa-brief", "T-1"])
    assert rc == 0
    index = json.loads(capsys.readouterr().out)
    assert "diff" not in index
    out = home / "derived" / "qa-brief" / "T-1"
    assert index["packet_path"] == str(out / "packet.json")
    patch = (out / "diff.patch").read_text(encoding="utf-8")
    assert patch.count("+line ") == 2000          # nothing truncated
    assert index["diff_lines"] == len(patch.splitlines())
    assert index["diff_bytes"] == len(patch.encode("utf-8"))
    assert json.loads((out / "packet.json").read_text(encoding="utf-8"))["diff"] == patch
    assert "attestation" not in json.dumps(index).lower()
