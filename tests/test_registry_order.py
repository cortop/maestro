"""Registries marked `# sorted-registry` stay one-entry-per-line and sorted, so two tickets adding an entry never conflict."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_MARKER = re.compile(r"^\s*#\s*sorted-registry(?::\s*after\s+(\d+))?\s*$")
_WRAPPERS = {"frozenset", "tuple", "set"}


def _key(node: ast.expr, src: str) -> str:
    """Sort key: source text, or the first positional argument's text for a Call element."""
    if isinstance(node, ast.Call) and node.args:
        node = node.args[0]
    return ast.get_source_segment(src, node) or ""


def check_source(src: str, where: str = "<src>") -> list[str]:
    """Return one problem string per violated or vacuous `# sorted-registry` marker in *src*."""
    problems = []
    tree = ast.parse(src)
    assigns = {n.lineno: n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))}
    for i, line in enumerate(src.splitlines(), start=1):
        m = _MARKER.match(line)
        if not m:
            continue
        label = f"{where}:{i}"
        node = assigns.get(i + 1)
        value = node.value if node is not None else None
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id in _WRAPPERS and len(value.args) == 1 and not value.keywords):
            value = value.args[0]
        if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            problems.append(f"{label}: marker is not immediately followed by a supported literal")
            continue
        elts = value.elts
        prev_end = value.lineno
        for e in elts:
            if e.lineno == prev_end and e is not elts[0] or (e is elts[0] and e.lineno == value.lineno):
                problems.append(f"{label}: entry {_key(e, src)} does not start on its own line")
            prev_end = e.end_lineno
        skip = int(m.group(1) or 0)
        keys = [_key(e, src) for e in elts[skip:]]
        if keys != sorted(keys):
            problems.append(f"{label}: entries are not sorted")
    return problems


def test_repo_registries_are_sorted_one_per_line():
    problems, markers = [], 0
    for path in [*ROOT.glob("maestro/**/*.py"), *ROOT.glob("tests/*.py")]:
        if path.name == Path(__file__).name:
            continue
        src = path.read_text()
        markers += len(re.findall(r"^\s*#\s*sorted-registry", src, re.M))
        problems += check_source(src, str(path.relative_to(ROOT)))
    assert not problems, "\n".join(problems)
    assert markers >= 5


def test_same_line_entries_fail():
    src = "# sorted-registry\nX = frozenset({\n    \"a\", \"b\",\n})\n"
    assert any("own line" in p for p in check_source(src))


def test_first_entry_on_opening_line_fails():
    src = "# sorted-registry\nX = (\"a\",\n     \"b\")\n"
    assert any("own line" in p for p in check_source(src))


def test_unsorted_entries_fail():
    src = "# sorted-registry\nX = (\n    \"b\",\n    \"a\",\n)\n"
    assert any("not sorted" in p for p in check_source(src))


def test_unsupported_literal_fails():
    assert check_source("# sorted-registry\nX = foo()\n")
    assert check_source("# sorted-registry\n\nX = (\n    \"a\",\n)\n")
    assert check_source("# sorted-registry\n")


def test_sorted_one_per_line_passes_and_unwraps():
    src = "# sorted-registry\nX = frozenset({\n    \"a\",\n    \"b\",\n})\n"
    assert check_source(src) == []
    assert check_source("# sorted-registry\nX = tuple([\n    \"a\",\n    \"b\",\n])\n") == []


def test_after_n_pins_unsorted_prefix():
    src = "# sorted-registry: after 1\nX = (\n    zz,\n    a,\n    b,\n)\n"
    assert check_source(src) == []
    assert check_source(src.replace("after 1", "after 0"))


def test_call_elements_sorted_by_first_argument():
    ok = "# sorted-registry\nX = (\n    K(\"a\", 9),\n    K(\"b\", 1),\n)\n"
    bad = "# sorted-registry\nX = (\n    K(\"b\", 1),\n    K(\"a\", 9),\n)\n"
    assert check_source(ok) == []
    assert check_source(bad)
