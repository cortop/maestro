"""Server-side Markdown rendering of untrusted ticket text for the web UI."""
from __future__ import annotations

import re

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

_MD = MarkdownIt("commonmark", {"html": False}).enable("table")
_COMMENT_LINE = re.compile(r"^\s*<!--.*?-->\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_TASK = re.compile(r"<li>(\s*<p>)?\[([ xX])\] ")


def _image(self, tokens, idx, options, env):
    """Images render as their alt text only; the URL is never emitted."""
    return str(escape(tokens[idx].content))


_MD.add_render_rule("image", _image)


def _strip_comments(text: str) -> str:
    """Drop whole-line `<!-- ... -->` comments outside fenced code blocks."""
    out, fence = [], None
    for line in text.splitlines():
        m = _FENCE.match(line)
        if m:
            marker = m.group(1)[0]
            fence = None if fence == marker else (fence or marker)
        elif fence is None and _COMMENT_LINE.match(line):
            continue
        out.append(line)
    return "\n".join(out)


def _task_box(m: re.Match) -> str:
    checked = " checked" if m.group(2) in "xX" else ""
    return f'<li class="task">{m.group(1) or ""}<input type="checkbox" disabled{checked}> '


def render(text: str | None) -> Markup:
    """HTML for *text*; raw HTML is escaped, unsafe link schemes are dropped, images become alt text."""
    if not (text or "").strip():
        return Markup("")
    html = _MD.render(_strip_comments(text))
    return Markup(_TASK.sub(_task_box, html))
