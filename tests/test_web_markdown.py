"""The Spec and Proposal tabs render untrusted Markdown server-side."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro.web.app import create_app  # noqa: E402

BODY = """# T-5: title

<!-- HUMAN-OWNED. Edit freely, anytime. -->

## Intent
- a bullet with `inline` code
- [ ] open item
- [x] done item

```python
x = 1
```

| a | b |
|---|---|
| 1 | 2 |

<script>alert(1)</script> and <i>x</i>
[x](javascript:alert(1)) ![alt text](http://evil.example/p.png)
"""


def doc(client, tab):
    """The rendered document element alone (the spec tab's editor textarea holds the raw source)."""
    html = client.get(f"/t/T-5/{tab}").text
    return html[html.index(f'<div id="{tab}" class="doc md">'):].split("</div>")[0]


@pytest.fixture
def client(home):
    seed_ticket(home, "T-5", "five", phase="implementing")
    (home / "tickets" / "T-5" / "spec.md").write_text(BODY, encoding="utf-8")
    (home / "tickets" / "T-5" / "proposal.md").write_text(BODY, encoding="utf-8")
    with TestClient(create_app(home)) as c:
        yield c


@pytest.mark.parametrize("tab", ["spec", "proposal"])
def test_tab_renders_markdown(client, tab):
    html = doc(client, tab)
    assert "<h2>Intent</h2>" in html and "## " not in html
    assert "<li>a bullet with <code>inline</code> code</li>" in html
    assert "<pre><code" in html and "x = 1" in html
    assert "<table>" in html


@pytest.mark.parametrize("tab", ["spec", "proposal"])
def test_raw_html_links_and_images_are_neutralised(client, tab):
    html = doc(client, tab)
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "&lt;i&gt;x&lt;/i&gt;" in html and "<i>x</i>" not in html
    assert 'href="javascript' not in html
    assert "<img" not in html and "evil.example" not in html and "alt text" in html


def test_human_owned_comment_hidden(client):
    assert "HUMAN-OWNED" not in doc(client, "spec")


@pytest.mark.parametrize("tab", ["spec", "proposal"])
def test_task_list_checkboxes_are_disabled(client, tab):
    html = doc(client, tab)
    assert '<input type="checkbox" disabled> open item' in html
    assert '<input type="checkbox" disabled checked> done item' in html


def test_missing_or_empty_shows_none(client, home):
    (home / "tickets" / "T-5" / "spec.md").write_text("  \n", encoding="utf-8")
    (home / "tickets" / "T-5" / "proposal.md").unlink()
    assert "(none)" in client.get("/t/T-5/spec").text
    assert "(none)" in client.get("/t/T-5/proposal").text
