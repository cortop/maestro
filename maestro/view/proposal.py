"""Proposal view-model: the answer phrases and option parse shared by the TUI and web proposal actions."""
from __future__ import annotations

import re

# Phrases the proposal decision bar sends; the awaiting-human skill routes on them
# (pinned by tests/test_reconcile_skill.py).
ANSWER_ALTERNATIVE = "alternative"
ANSWER_NEEDS_MORE = "needs more"

_ALT_HEADING_RE = re.compile(r"^##\s+Alternative\s+(\d+)\b", re.IGNORECASE)


def qid(key: str) -> str:
    """The question id of a ticket's research-approval ask."""
    return f"research-approval-{key}"


def parse_proposal_options(text: str) -> list[tuple[str, str]]:
    """`[(label, title)]` for `## Recommended` and every `## Alternative N` in proposal.md.
    Label is `Recommended` or `N`; title is the section's first non-empty line."""
    out: list[tuple[str, str]] = []
    label: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            m = _ALT_HEADING_RE.match(line)
            if m:
                label = m.group(1)
            elif line[3:].strip().lower().startswith("recommended"):
                label = "Recommended"
            else:
                label = None
            if label is not None:
                out.append((label, ""))
        elif label is not None and line.strip() and not out[-1][1]:
            out[-1] = (label, line.strip().lstrip("#").strip())
    return out
