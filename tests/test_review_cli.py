"""`maestro reviews` over a real temp home, plus the import discipline."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from maestro import cli
from tests.conftest import seed_ticket
from tests.test_review_model import seed


def run(home, capsys, *argv):
    code = cli.main(["--home", str(home), *argv])
    return code, capsys.readouterr().out


def test_reviews_json_lists_only_reviews(home, capsys):
    seed(home, "r-1", "qa")
    seed(home, "r-2", "done", reason="merged")
    seed_ticket(home, "T-1", "regular", phase="ready")
    code, out = run(home, capsys, "reviews", "--json")
    assert code == 0 and [r["key"] for r in json.loads(out)] == ["r-1"]
    code, out = run(home, capsys, "reviews", "--json", "--all")
    assert sorted(r["key"] for r in json.loads(out)) == ["r-1", "r-2"]


def test_reviews_show(home, capsys):
    seed(home, "r-1", "qa")
    code, out = run(home, capsys, "reviews", "show", "r-1", "--json")
    assert code == 0 and json.loads(out)["status"] == "reviewing"
    seed_ticket(home, "T-1", "regular", phase="ready")
    assert run(home, capsys, "reviews", "show", "T-1")[0] == 2
    assert run(home, capsys, "reviews")[0] == 0


@pytest.mark.parametrize("first", ["maestro.dispatcher", "maestro.ops", "maestro.notify",
                                   "maestro.review"])
def test_import_order(first):
    code = (f"import sys, {first}\nimport maestro.dispatcher, maestro.ops, maestro.notify, "
            "maestro.review\n")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_review_import_is_lean():
    code = ("import sys\nbase=set(sys.modules)\nimport maestro.review\n"
            "bad=[m for m in set(sys.modules)-base if m.split('.')[0] in "
            "('fastapi','jinja2','textual','rich','starlette','uvicorn','yaml')]\n"
            "assert not bad, bad\n")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
