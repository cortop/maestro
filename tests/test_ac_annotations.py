"""T-79: opt-in, machine-checkable AC annotations -- the parser + content-hash
identity half. `(test: <path>[::<id>])` / `(check: <shell command>)` trailing an
AC checkbox line is folded into the SAME whole-line hash every other AC uses
(`snapshot.ac_hash`), so nothing downstream needs to special-case it for
identity purposes -- editing/removing the annotation invalidates prior
attestations/verdicts/checks exactly like any other edit to the line.
"""
from __future__ import annotations

from pathlib import Path

from maestro import snapshot as snap_mod
from maestro.snapshot import AcAnnotation, ac_hash, parse_ac_annotation, parse_acs


# ---------------------------------------------------------------------------
# AC1: the parser recognizes the three forms, and any OTHER trailing
# parenthetical stays plain text.
# ---------------------------------------------------------------------------

def test_parses_bare_test_path_annotation():
    ann = parse_ac_annotation("widget builds (test: tests/test_widget.py)")
    assert ann == AcAnnotation(kind="test", raw="tests/test_widget.py", path="tests/test_widget.py")


def test_parses_test_path_with_node_id():
    ann = parse_ac_annotation("widget builds (test: tests/test_widget.py::test_build)")
    assert ann == AcAnnotation(kind="test", raw="tests/test_widget.py::test_build",
                               path="tests/test_widget.py", test_id="test_build")


def test_parses_test_path_with_class_scoped_node_id():
    ann = parse_ac_annotation("widget builds (test: tests/test_widget.py::TestWidget::test_build)")
    assert ann.kind == "test"
    assert ann.path == "tests/test_widget.py"
    assert ann.test_id == "TestWidget::test_build"


def test_parses_check_annotation():
    ann = parse_ac_annotation("docs regenerate (check: make diagram --check)")
    assert ann == AcAnnotation(kind="check", raw="make diagram --check", command="make diagram --check")


def test_other_trailing_parenthetical_is_not_an_annotation():
    assert parse_ac_annotation("widget builds (checked manually)") is None
    assert parse_ac_annotation("widget builds (see #123)") is None
    assert parse_ac_annotation("widget builds") is None


def test_a_second_trailing_parenthetical_after_the_annotation_is_not_swallowed():
    """A body may not contain unescaped parens -- this is what keeps a SECOND
    trailing parenthetical from being engulfed into the annotation's body."""
    ann = parse_ac_annotation("widget builds (test: tests/test_widget.py) (see #123)")
    assert ann is None  # the whole thing no longer ends in `(test: ...)` / `(check: ...)`


def test_empty_annotation_body_is_not_an_annotation():
    assert parse_ac_annotation("widget builds (test: )") is None
    assert parse_ac_annotation("widget builds (check:)") is None


# ---------------------------------------------------------------------------
# T-98 AC6: the body is found by a paren-BALANCED scan, not a `[^()]` ban --
# a `check:` command containing its own parens (a Bazel `--test_filter`
# regex) parses correctly instead of silently degrading to the prose tier.
# ---------------------------------------------------------------------------

def test_check_annotation_with_balanced_parens_in_the_body_parses():
    ann = parse_ac_annotation(
        "widget adds (check: bzl test //src/widget:widget_test --test_filter='^(TestA|TestB)$')")
    assert ann.kind == "check"
    assert ann.command == "bzl test //src/widget:widget_test --test_filter='^(TestA|TestB)$'"


def test_check_annotation_with_nested_balanced_parens_parses():
    ann = parse_ac_annotation("docs regenerate (check: (cd sub && make check))")
    assert ann.kind == "check"
    assert ann.command == "(cd sub && make check)"


def test_check_annotation_with_unbalanced_parens_does_not_parse():
    """An unbalanced body never has a well-defined end -- this must decline to
    parse (never silently truncate at the first stray `)`), and is exactly
    the shape `health.check_ac_annotation_parse` (T-98) flags."""
    assert parse_ac_annotation("widget adds (check: bzl test --test_filter='^(TestA')") is None


# ---------------------------------------------------------------------------
# AC1 (continued): parse_acs stays byte-identical to before this ticket, on a
# copy of a REAL existing spec (T-55's -- no annotations at all).
# ---------------------------------------------------------------------------

_T55_SPEC = """\
# T-55: PI-3 — Generalize the spawn preflight off ollama into a per-runner probe table

<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->

approval_tier: 2
priority: 2
kind: implementation
dependsOn: [T-53]

runner: claude
## Intent
Refactor only: opencode's behaviour must be byte-identical afterward.

## Notes
- Settle the probe signature here, deliberately.

## Acceptance criteria
- [ ] The default probe dispatches through a `{runner name: probe fn}` table and falls back to the ollama probe for an unrecognized name.
- [ ] Every existing test in `tests/test_runner_preflight.py`, `tests/test_runner_enabled.py`, `tests/test_runner_opencode_concurrency.py` and `tests/test_runner_opencode_routing.py` passes with **no test-file edits** — the injected one-arg `runner_probe` still overrides every runner's probe at once.
- [ ] The transient daemon-unreachable blocker text is derived from the probe result, not the literal string: a real sweep with an injected probe for a non-ollama runner produces a `runner_blockers` entry that does not contain the substring `ollama`.
- [ ] Probe caching is once-per-sweep-per-runner-name: a real sweep over a board with three due tickets on the same non-claude runner calls the injected probe exactly once.
- [ ] `make test` green.
"""


def test_unannotated_real_spec_parses_byte_identically():
    acs = parse_acs(_T55_SPEC)
    assert len(acs) == 5
    # No annotation anywhere in a genuinely unannotated spec.
    assert all(parse_ac_annotation(t) is None for t in acs)
    # ac_hash is computed straight off the parsed text, exactly as before this
    # ticket -- the annotation machinery adds a parallel lookup, never changes
    # what parse_acs/ac_hash themselves return.
    assert [ac_hash(t) for t in acs] == [snap_mod.content_hash(t.strip()) for t in acs]


# ---------------------------------------------------------------------------
# AC2: annotations ride the existing whole-line content hash -- editing or
# removing one invalidates identity exactly like any other line edit.
# ---------------------------------------------------------------------------

def test_editing_the_annotation_changes_the_ac_hash():
    unannotated = "widget builds"
    with_test = "widget builds (test: tests/test_widget.py)"
    with_check = "widget builds (check: true)"
    assert ac_hash(unannotated) != ac_hash(with_test)
    assert ac_hash(with_test) != ac_hash(with_check)
    assert ac_hash(with_test) != ac_hash("widget builds (test: tests/test_widget.py::test_build)")


def test_removing_the_annotation_reproduces_the_unannotated_hash():
    text = "widget builds (test: tests/test_widget.py)"
    stripped = "widget builds"
    assert ac_hash(text) != ac_hash(stripped)


def test_fold_invalidates_ac_verified_when_annotation_is_added(home):
    """The AC2 fold test: an AcVerified attestation against the unannotated
    line no longer matches once a human edit adds an annotation -- proven
    through a real fold, not just the hash comparison above."""
    from maestro import event_log, ops
    from maestro.config import Config

    key = "F-1"
    unannotated = "widget builds"
    text_v1 = f"# F-1: t\n\n## Intent\nx\n\n## Acceptance criteria\n- [ ] {unannotated}\n"
    from maestro import store
    store.atomic_write(store.spec_path(home, key), text_v1)
    event_log.append(home, key, "TicketCreated", {"title": "t", "source": "test"}, actor="d")
    from maestro import snapshot as snap_mod2
    snap_mod2.rebuild(home, key)
    cfg = Config(home=home)
    h_before = ac_hash(unannotated)
    ops.verify_ac(cfg, key, 1, {"what": "ran it", "where": "test", "result": "ok"})
    snap = snap_mod2.load(home, key)
    assert snap.acs_unverified(text_v1) == 0  # freshly attested

    annotated = "widget builds (test: tests/test_widget.py)"
    text_v2 = f"# F-1: t\n\n## Intent\nx\n\n## Acceptance criteria\n- [ ] {annotated}\n"
    store.atomic_write(store.spec_path(home, key), text_v2)
    snap = snap_mod2.load(home, key)
    # Annotation regime inactive (no tree_key passed) -- falls back to plain
    # attestation, which the edit has already desynced (different hash).
    assert snap.acs_unverified(text_v2) == 1
    assert ac_hash(annotated) != h_before
