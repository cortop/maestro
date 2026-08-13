"""Drift + idempotency guards for the generated docs/state-machine.md and
docs/dispatch-gates.md (T-50): both are pure derivations of maestro/statemachine.py
and an AST walk of maestro/dispatcher.py, never hand-retyped -- these tests are what
makes forgetting `make diagram` after touching either source fail `make test`."""
from __future__ import annotations

import subprocess
from pathlib import Path

from maestro import diagram

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Drift guard: committed docs/*.md must equal a fresh in-memory regeneration.
# ---------------------------------------------------------------------------

def test_state_machine_doc_matches_generator():
    committed = diagram.STATE_DIAGRAM_PATH.read_text()
    generated = diagram.render_state_diagram()
    assert generated == committed, (
        "docs/state-machine.md is stale -- maestro/statemachine.py changed since "
        "it was last generated; run `make diagram` and commit the result")


def test_dispatch_gates_doc_matches_generator():
    committed = diagram.DISPATCH_GATES_PATH.read_text()
    generated = diagram.render_dispatch_gates()
    assert generated == committed, (
        "docs/dispatch-gates.md is stale -- maestro/dispatcher.py's "
        "decisions[key][\"outcome\"] literals changed since it was last generated; "
        "run `make diagram` and commit the result")


# ---------------------------------------------------------------------------
# Content sanity: the generated docs actually describe what they claim to.
# ---------------------------------------------------------------------------

def test_state_diagram_covers_every_phase_and_is_valid_mermaid():
    text = diagram.STATE_DIAGRAM_PATH.read_text()
    assert "```mermaid" in text and "stateDiagram-v2" in text
    for phase in diagram.statemachine.Phase:
        assert phase.value in text, f"{phase.value} missing from docs/state-machine.md"


def test_dispatch_gates_row_count_matches_source_today():
    """Not a hardcoded magic number -- cross-checks the table's own row count
    against a fresh AST walk of the live dispatcher.py, so this test itself
    can never drift from the source it's proving the doc matches."""
    rows = diagram._outcome_assignments(diagram.DISPATCHER_SOURCE_PATH.read_text())
    text = diagram.DISPATCH_GATES_PATH.read_text()
    assert f"{len(rows)} gates today." in text
    for _, outcome in rows:
        assert f"`{outcome}`" in text


def test_outcome_assignments_ast_walk_matches_real_dispatcher_source():
    """Proof the AST walk finds real decisions[key]["outcome"] = "<literal>"
    assignments, not an artifact of a hand-crafted fixture."""
    source = diagram.DISPATCHER_SOURCE_PATH.read_text()
    rows = diagram._outcome_assignments(source)
    assert len(rows) >= 10, "expected the dispatcher's real gate table, not a stub"
    linenos = [lineno for lineno, _ in rows]
    assert linenos == sorted(linenos), "rows must be in source order"
    # Every literal genuinely appears at its claimed line in the source.
    src_lines = source.splitlines()
    for lineno, outcome in rows:
        assert f'"{outcome}"' in src_lines[lineno - 1]


# ---------------------------------------------------------------------------
# Idempotency: regenerating never perturbs output -- mirrors how
# tests/test_autocomplete.py:131-180 tests `make autocomplete`.
# ---------------------------------------------------------------------------

def test_generator_output_is_idempotent_in_process():
    assert diagram.render_state_diagram() == diagram.render_state_diagram()
    assert diagram.render_dispatch_gates() == diagram.render_dispatch_gates()


def test_make_diagram_is_idempotent(tmp_path):
    """Running the real `make diagram` target twice against an isolated
    output directory yields byte-identical files both times -- the real CLI
    surface, not just the pure render_*() functions in-process."""
    out_dir = tmp_path / "docs"
    contents = []
    for _ in range(2):
        result = subprocess.run(
            [".venv/bin/python", "-m", "maestro.diagram", "--docs-dir", str(out_dir)],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        assert result.returncode == 0, f"maestro.diagram failed:\n{result.stderr}"
        contents.append({
            "state-machine.md": (out_dir / "state-machine.md").read_text(),
            "dispatch-gates.md": (out_dir / "dispatch-gates.md").read_text(),
        })
    assert contents[0] == contents[1]
