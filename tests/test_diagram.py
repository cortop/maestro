"""Drift + idempotency guards for the generated docs/state-machine.md and
docs/dispatch-gates.md (T-50): both are pure derivations of maestro/statemachine.py
and an AST walk of maestro/dispatcher.py, never hand-retyped -- these tests are what
makes forgetting `make diagram` after touching either source fail `make test`."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from maestro import diagram

REPO_ROOT = Path(__file__).resolve().parents[1]

# T-93: an independent regex scan of dispatcher.py's `decisions[key]` outcome
# sites -- deliberately NOT built on `diagram._outcome_assignments`, so that
# extractor agreeing with itself can no longer be mistaken for the doc being
# complete. Two patterns, matching the two assignment forms `_outcome_assignments`
# now widens across: `decisions[key]["outcome"] = "<lit>"`, and a
# `"outcome": "<lit>"` dict-literal key (optionally a `<a> if <cond> else <b>`
# conditional, whose second literal is capture group 2).
_SUBSCRIPT_OUTCOME_RE = re.compile(r'decisions\[key\]\["outcome"\]\s*=\s*"([a-zA-Z_]+)"')
_DICT_OUTCOME_RE = re.compile(
    r'"outcome":\s*"([a-zA-Z_]+)"(?:\s+if\s+.+?\s+else\s+"([a-zA-Z_]+)")?')


def _independent_outcome_literals(source: str) -> set[str]:
    literals = set(_SUBSCRIPT_OUTCOME_RE.findall(source))
    for first, second in _DICT_OUTCOME_RE.findall(source):
        literals.add(first)
        if second:
            literals.add(second)
    return literals


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
        # A real hyphenated phase name (e.g. "awaiting-human") used as a bare
        # mermaid state id -- not just quoted as a `state "..." as ...` alias
        # label -- fails the real mermaid parser (confirmed against mermaid
        # v11.16.1, the engine GitHub's renderer uses: a bare hyphenated id
        # collides with the `-->` arrow tokenizer). Every phase must instead
        # be aliased to `diagram._safe_id(phase.value)`, which never contains
        # a hyphen -- guard both halves of that contract structurally, since
        # this suite can't assume a JS/mermaid toolchain is available.
        alias = diagram._safe_id(phase.value)
        assert f'state "{phase.value}" as {alias}' in text, (
            f"{phase.value} must be aliased via `state \"...\" as {alias}`, "
            "not used as a bare (possibly hyphenated) mermaid state id")
        assert "-" not in alias, f"_safe_id({phase.value}!r) still contains a hyphen"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("state ", "classDef", "%%", "```", "#", "<!--")) or not stripped:
            continue
        if "-->" in stripped:
            src, dst = (tok.strip() for tok in stripped.split("-->", 1))
            assert "-" not in src and "-" not in dst, (
                f"bare hyphenated state id in transition line {line!r} -- mermaid's "
                "parser rejects a hyphen in an unquoted state id")


def test_dispatch_gates_row_count_matches_source_today():
    """Not a hardcoded magic number -- cross-checks the table's own row count
    against a fresh AST walk of the live dispatcher.py, so this test itself
    can never drift from the source it's proving the doc matches."""
    rows = diagram._outcome_assignments(diagram.DISPATCHER_SOURCE_PATH.read_text())
    text = diagram.DISPATCH_GATES_PATH.read_text()
    assert f"{len(rows)} gates today." in text
    for _, outcome in rows:
        assert f"`{outcome}`" in text


def test_dispatch_gates_outcome_set_is_complete_independent_of_extractor():
    """T-93: the doc's outcome set is validated against `_independent_outcome_literals`
    (a plain regex scan, above) instead of `diagram._outcome_assignments` -- so this
    test cannot pass merely because the generator agrees with itself the way
    `test_dispatch_gates_row_count_matches_source_today` (which reuses the same
    extractor) always would. Deleting the dict-literal branch of the widened AST walk
    in `diagram._outcome_assignments` drops `phantom`/`fold_error`/`not_due`/
    `missing_acs`/`would_park_missing_acs`/`claimed`/`due` from the generated table,
    and this test -- unlike the extractor-reusing one -- catches it."""
    source = diagram.DISPATCHER_SOURCE_PATH.read_text()
    expected = _independent_outcome_literals(source)
    assert len(expected) >= 10, "expected a real outcome set, not an empty/stub regex match"
    text = diagram.DISPATCH_GATES_PATH.read_text()
    missing = sorted(o for o in expected if f"`{o}`" not in text)
    assert not missing, f"outcome(s) missing from docs/dispatch-gates.md: {missing}"


def test_dispatch_gates_records_the_four_sweep_level_brakes():
    """T-93: fleet pause, the rate-limit pause, the runaway auto-brake and the
    spend ceiling all short-circuit the spawn phase before any per-key outcome
    reaches `spawned`/`would_spawn` -- none of them has a `decisions[key]["outcome"]`
    entry of its own, so the outcome table above can never mention them; this is
    the only place they're recorded at all."""
    text = diagram.DISPATCH_GATES_PATH.read_text()
    for name in ("fleet pause", "rate-limit pause", "runaway auto-brake", "spend ceiling"):
        assert name in text, f"sweep-level brake {name!r} missing from docs/dispatch-gates.md"


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
            [sys.executable, "-m", "maestro.diagram", "--docs-dir", str(out_dir)],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        assert result.returncode == 0, f"maestro.diagram failed:\n{result.stderr}"
        contents.append({
            "state-machine.md": (out_dir / "state-machine.md").read_text(),
            "dispatch-gates.md": (out_dir / "dispatch-gates.md").read_text(),
        })
    assert contents[0] == contents[1]
