"""The implementing skill is repo-neutral; maestro-only rules live in CLAUDE.md."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".claude" / "commands" / "maestro-reconcile-implementing.md"


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_nearest_agents_md_rule():
    text = _flat(SKILL)
    assert "AGENTS.md`/`CLAUDE.md` nearest each directory you edit" in text
    assert "exactly as the repo's CLAUDE.md/AGENTS.md says" in text


def test_maestro_rules_moved_to_claude_md():
    skill, claude = _flat(SKILL), _flat(ROOT / "CLAUDE.md")
    for gone in (".venv/bin/python -m pytest", "Pytest's permission story",
                 "# T-NNN:", "In this repo", "test_tui_<feature>.py"):
        assert gone not in skill, gone
    for kept in ("1800000ms", "BASH_MAX_TIMEOUT_MS", "`run_in_background`", "ScheduleWakeup",
                 ".venv/bin/python -m pytest -q", "*relative* prefix `.venv/bin/`",
                 "dispatcher._worker_cwd", "test_tui_<feature>.py",
                 "test_every_binding_action_resolves", "docs/dispatch-gates.md",
                 "docs/state-machine.md", ".venv/bin/python -m maestro.diagram", "# T-NNN:"):
        assert kept in claude, kept
