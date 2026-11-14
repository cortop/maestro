"""Web Tickets page: search, column sort, the dependency view and the ticket's `blocks` list."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import store  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


def _order(html: str) -> list[str]:
    return re.findall(r'<(?:tr class="row"|li) (?:class="[^"]*" )?data-key="([^"]+)"', html)


@pytest.fixture
def web(home):
    seed_ticket(home, "T-1", "Alpha Widget", phase="implementing", pr=30)
    seed_ticket(home, "T-2", "beta thing", phase="ready")
    seed_ticket(home, "T-10", "Gamma widget", phase="implementing", pr=5)
    seed_ticket(home, "T-3", "Done one", phase="done")
    for key, dep in (("T-2", "T-1"), ("T-10", "T-1")):
        spec = store.spec_path(home, key)
        spec.write_text(f"---\npriority: 2\ndependsOn: [{dep}]\n---\n" + spec.read_text(encoding="utf-8"),
                        encoding="utf-8")
    with TestClient(create_app(home)) as c:
        yield c


def test_search_matches_key_or_title_case_insensitively_within_filter(web):
    assert _order(web.get("/board/rows?filter=all&q=WIDGET").text) == ["T-1", "T-10"]
    assert _order(web.get("/board/rows?filter=all&q=t-2").text) == ["T-2"]
    # combined with the active filter: the done ticket matches `t-` but `active` hides it
    assert _order(web.get("/board/rows?filter=all&q=t-3").text) == ["T-3"]
    assert _order(web.get("/board/rows?filter=active&q=t-3").text) == []
    assert _order(web.get("/board/rows?filter=review&q=t-").text) == []
    assert "and search" in web.get("/board/rows?filter=all&q=zzz").text


def test_search_is_kept_in_url_and_on_refresh_urls(web):
    html = web.get("/tickets?filter=all&q=widget&sort=title&dir=desc").text
    assert _order(html) == ["T-10", "T-1"]
    assert 'hx-get="/board/rows?filter=all&amp;q=widget&amp;sort=title&amp;dir=desc"' in html
    assert 'name="q" value="widget"' in html
    assert 'href="/tickets?filter=active&amp;q=widget&amp;sort=title&amp;dir=desc"' in html


def test_header_click_sorts_and_second_click_reverses(web):
    html = web.get("/tickets?filter=all").text
    assert 'href="/tickets?filter=all&amp;sort=title&amp;dir=asc"' in html
    asc = _order(web.get("/tickets?filter=all&sort=key&dir=asc").text)
    assert asc == ["T-1", "T-2", "T-3", "T-10"]  # natural key order, not lexical
    page = web.get("/tickets?filter=all&sort=key&dir=asc").text
    assert 'href="/tickets?filter=all&amp;sort=key&amp;dir=desc"' in page  # second click reverses
    assert _order(web.get("/tickets?filter=all&sort=key&dir=desc").text) == asc[::-1]
    assert 'aria-sort="descending"' in web.get("/tickets?filter=all&sort=key&dir=desc").text


def test_numeric_column_first_sorts_descending_and_unknown_sort_is_400(web):
    assert 'href="/tickets?filter=all&amp;sort=pr&amp;dir=desc"' in web.get("/tickets?filter=all").text
    assert _order(web.get("/board/rows?filter=all&sort=pr&dir=desc").text)[:2] == ["T-1", "T-10"]
    assert web.get("/board/rows?filter=all&sort=nope").status_code == 400


def test_dependency_view_is_a_linked_tree_with_blocking_depth(web):
    html = web.get("/deps").text
    assert _order(html) == ["T-1", "T-2", "T-10"]  # root first, its dependents beneath
    assert "T-3" not in html  # terminal tickets are not open
    assert 'data-key="T-1" data-level="0"' in html and 'data-key="T-2" data-level="1"' in html
    for key in ("T-1", "T-2", "T-10"):
        assert f'<a class="dep-row-key" href="/t/{key}">' in html
    assert re.search(r'data-key="T-1".*?<i class="dep-dot"></i>T-1', html, re.S)
    assert re.search(r'data-key="T-2".*?<i class="dep-dot d1"></i>T-2', html, re.S)
    assert 'href="/deps"' in web.get("/tickets").text
    assert "Open dependency view" in web.get("/palette").text


def test_ticket_page_lists_tickets_it_blocks(web):
    side = web.get("/t/T-1/side").text
    assert re.search(r'id="blocks">.*<a href="/t/T-2">T-2</a>.*<a href="/t/T-10">T-10</a>', side, re.S)
    assert re.search(r'id="blocks">—', web.get("/t/T-2/side").text)
    assert 'id="blocks"' in web.get("/t/T-1").text
