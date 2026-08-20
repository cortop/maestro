"""T-84: the per-language added/deleted test-name extractor + selector
formatter table `ops.run_ac_checks`/`dispatcher`'s H4 gate both select from.
Unit-level: pure regex/formatter behavior, no subprocess, no worktree --
the end-to-end wiring (a real dispatcher sweep per language) lives in
`tests/test_dispatcher_ac_checks_lang.py`.
"""
from __future__ import annotations

import pytest

from maestro import testlang


def test_unconfigured_language_resolves_to_python():
    assert testlang.resolve(None) is testlang.PYTHON
    assert testlang.resolve("") is testlang.PYTHON


def test_unsupported_language_raises():
    with pytest.raises(testlang.UnsupportedLanguage, match="rust"):
        testlang.resolve("rust")


def test_supported_languages_all_resolve():
    for name in testlang.SUPPORTED:
        assert testlang.resolve(name).name == name


# ---------------------------------------------------------------------------
# python -- byte-identical to the pre-T-84 sole regex/selector.
# ---------------------------------------------------------------------------

def test_python_added_re_matches_a_plus_def_line():
    m = testlang.PYTHON.added_re.match("+def test_widget():")
    assert m and m.group(1) == "test_widget"
    assert testlang.PYTHON.added_re.match("+def helper():") is None
    assert testlang.PYTHON.added_re.match(" def test_widget():") is None  # no leading +


def test_python_selector_bare_and_id_form():
    assert (testlang._python_selector("pytest -q", "tests/test_w.py", ["test_a", "test_b"])
           == "pytest -q tests/test_w.py::test_a tests/test_w.py::test_b")
    assert (testlang._python_selector("pytest -q", "tests/test_w.py", ["Cls::test_m"])
           == "pytest -q tests/test_w.py::Cls::test_m")


# ---------------------------------------------------------------------------
# go
# ---------------------------------------------------------------------------

def test_go_added_re_matches_a_plus_func_test_line():
    m = testlang.GO.added_re.match("+func TestWidget(t *testing.T) {")
    assert m and m.group(1) == "TestWidget"
    assert testlang.GO.added_re.match("+func helper() {") is None


def test_go_line_re_captures_sign_and_name_for_deletion_scan():
    m = testlang.GO.line_re.match("-func TestWidget(t *testing.T) {")
    assert m and m.group(1) == "-" and m.group(2) == "TestWidget"
    m = testlang.GO.line_re.match("+func TestWidget(t *testing.T) {")
    assert m and m.group(1) == "+" and m.group(2) == "TestWidget"


def test_go_selector_uses_package_dir_and_run_flag():
    cmd = testlang.GO.format_selector("go test", "internal/widget/widget_test.go", ["TestWidget"])
    assert cmd == "go test ./internal/widget -run '^(TestWidget)$'"


def test_go_selector_top_level_file_has_no_subdir():
    cmd = testlang.GO.format_selector("go test", "widget_test.go", ["TestWidget"])
    assert cmd == "go test . -run '^(TestWidget)$'"


def test_go_selector_joins_multiple_names_as_alternation():
    cmd = testlang.GO.format_selector("go test", "internal/widget/widget_test.go",
                                      ["TestAlpha", "TestBeta"])
    assert cmd == "go test ./internal/widget -run '^(TestAlpha|TestBeta)$'"


# ---------------------------------------------------------------------------
# typescript
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_typescript_added_re_matches_it_and_test_with_any_quote_style(quote):
    for kw in ("it", "test"):
        line = f"+{kw}({quote}does the thing{quote}, () => {{"
        m = testlang.TYPESCRIPT.added_re.match(line)
        assert m and m.group(1) == "does the thing"


def test_typescript_selector_uses_dash_t_flag():
    cmd = testlang.TYPESCRIPT.format_selector("jest", "src/widget.spec.ts", ["does_the_thing"])
    assert cmd == 'jest src/widget.spec.ts -t "does_the_thing"'


def test_typescript_test_file_globs_cover_spec_and_test_and_dunder_tests_dir():
    assert "*.spec.ts" in testlang.TYPESCRIPT.test_file_globs
    assert "*.test.ts" in testlang.TYPESCRIPT.test_file_globs
    assert "__tests__/" in testlang.TYPESCRIPT.test_file_globs
