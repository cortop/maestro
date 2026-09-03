"""Linear adapter — opt-in ticket source (``tracker = "linear"``).

Talks to Linear's GraphQL API over ``urllib.request`` only (stdlib, no third-party
dep, same constraint as ``jira.py``). Two directions, both funneled through the
audited channels the rest of maestro already uses: ``import_new`` enqueues via
``inbox.append_new``; ``refresh`` appends ``LinearSynced``/``Note`` events for a
tracked, not-done ticket.

Linear identifies issues two ways: an opaque UUID (``id``) and a human-readable
``identifier`` (e.g. ``ENG-123``). maestro uses ``identifier`` as the external id —
it is what a human recognizes and what ``LINEAR-<identifier>`` keys are built from,
mirroring how ``jira.py`` uses the Jira issue key.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from .. import event_log, events as E, inbox, store
from ..statemachine import Phase, _assert_exhaustive

# A bare Linear identifier: team key + dash + issue number, e.g. "ENG-123".
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


def parse_identifier(url_or_id: str) -> str:
    """Accept either a bare Linear identifier (``ENG-123``) or a full issue URL
    (``https://linear.app/<org>/issue/ENG-123/some-slug``) and return the bare,
    uppercased identifier -- what ``LINEAR-<identifier>`` keys are built from
    (T-103). Raises ``store.MaestroError`` -- never a raw traceback -- for
    anything else, so a pasted typo surfaces as a clear CLI/TUI error."""
    s = (url_or_id or "").strip()
    if _ID_RE.match(s):
        return s.upper()
    parsed = urlparse(s)
    if parsed.scheme in ("http", "https") and parsed.netloc.endswith("linear.app"):
        parts = [p for p in parsed.path.split("/") if p]
        if "issue" in parts:
            idx = parts.index("issue")
            if idx + 1 < len(parts) and _ID_RE.match(parts[idx + 1]):
                return parts[idx + 1].upper()
    raise store.MaestroError(f"not a Linear issue URL or identifier: {url_or_id!r}")


class LinearTransport(Protocol):
    """The external HTTP boundary — the only thing a test should fake."""

    def search_issues(self, filter: dict) -> list[dict]: ...
    def get_issue(self, identifier: str) -> dict: ...
    def get_comments(self, identifier: str) -> list[dict]: ...
    def get_workflow_states(self, identifier: str) -> dict: ...
    def update_issue_state(self, issue_id: str, state_id: str) -> bool: ...


_ISSUES_QUERY = """
query Issues($filter: IssueFilter) {
  issues(filter: $filter) {
    nodes { id identifier title description state { name } updatedAt assignee { name } }
  }
}
"""

_ISSUE_QUERY = """
query Issue($id: String!) {
  issue(id: $id) { id identifier title description state { name } updatedAt assignee { name } }
}
"""

_COMMENTS_QUERY = """
query Comments($id: String!) {
  issue(id: $id) { comments { nodes { id body createdAt } } }
}
"""

# T-104: workflow states are per-TEAM objects in Linear, not global strings --
# resolving one by name means fetching the issue's own team's states, not a
# board-wide list.
_TEAM_STATES_QUERY = """
query IssueTeamStates($id: String!) {
  issue(id: $id) { id team { states { nodes { id name } } } }
}
"""

_UPDATE_STATE_MUTATION = """
mutation UpdateIssueState($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) { success }
}
"""


class HttpLinearTransport:
    """Real transport: personal API key auth, JSON-over-GraphQL via urllib."""

    def __init__(self, api_key: str):
        self._headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, query: str, variables: dict) -> dict:
        data = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            "https://api.linear.app/graphql", data=data, headers=self._headers, method="POST")
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    body = json.loads(raw) if raw else {}
                    if body.get("errors"):
                        raise RuntimeError(f"Linear API error: {body['errors']}")
                    return body.get("data") or {}
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 4:
                    retry_after = int(e.headers.get("Retry-After", "5"))
                    time.sleep(retry_after)
                    continue
                raise

    def search_issues(self, filter: dict) -> list[dict]:
        data = self._request(_ISSUES_QUERY, {"filter": filter})
        return ((data.get("issues") or {}).get("nodes")) or []

    def get_issue(self, identifier: str) -> dict:
        return self._request(_ISSUE_QUERY, {"id": identifier}).get("issue") or {}

    def get_comments(self, identifier: str) -> list[dict]:
        issue = self._request(_COMMENTS_QUERY, {"id": identifier}).get("issue") or {}
        return ((issue.get("comments") or {}).get("nodes")) or []

    def get_workflow_states(self, identifier: str) -> dict:
        issue = self._request(_TEAM_STATES_QUERY, {"id": identifier}).get("issue") or {}
        states = ((issue.get("team") or {}).get("states") or {}).get("nodes") or []
        return {"issue_id": issue.get("id"), "states": states}

    def update_issue_state(self, issue_id: str, state_id: str) -> bool:
        data = self._request(_UPDATE_STATE_MUTATION, {"id": issue_id, "stateId": state_id})
        return bool((data.get("issueUpdate") or {}).get("success"))


# T-104 (RB-9): one exhaustive, hand-decided row per `Phase` -- the Linear
# workflow-state NAME to push when a Linear-linked ticket enters that phase,
# or `None` to push nothing. Mirrors `statemachine.PHASE_CLASS`'s house
# pattern (right down to reusing its `_assert_exhaustive`): a newly added
# `Phase` must get an explicit row here before it ships, never silently fall
# through to "push nothing" by omission.
STATUS_BY_PHASE: dict[Phase, str | None] = {
    Phase.TRIAGING: None,
    Phase.AWAITING_HUMAN: None,
    Phase.READY: "To do",
    Phase.IMPLEMENTING: "In Progress",
    Phase.VERIFYING: None,
    Phase.QA: None,
    Phase.RESEARCHING: None,
    Phase.AWAITING_CI: "In Review",
    Phase.IN_REVIEW: "In Review",
    Phase.DEGRADED: None,
    Phase.TERMINATING: None,
    Phase.DONE: "Done",
}
_assert_exhaustive(Phase, STATUS_BY_PHASE)


class LinearTracker:
    """Implements the ``Tracker`` protocol plus the import/refresh sync half."""

    def __init__(self, settings: dict, transport: LinearTransport | None = None):
        self.settings = settings
        self.import_filter = settings.get(
            "import_filter",
            {"assignee": {"isMe": {"eq": True}}, "state": {"type": {"nin": ["completed", "canceled"]}}},
        )
        self.sync_interval = int(settings.get("sync_interval", 900))
        self._transport = transport

    def _transport_or_build(self) -> LinearTransport:
        if self._transport is not None:
            return self._transport
        api_key_env = self.settings.get("api_key_env", "LINEAR_API_KEY")
        import os
        api_key = os.environ.get(api_key_env, "")
        self._transport = HttpLinearTransport(api_key)
        return self._transport

    # --- Tracker protocol -------------------------------------------------
    def view(self, key: str) -> dict:
        return self._transport_or_build().get_issue(key)

    def transition(self, key: str, status: str) -> None:
        """T-104: push *status* (a Linear workflow-state NAME, e.g. "In
        Progress") onto issue *key* via the real ``issueUpdate`` GraphQL
        mutation. Workflow states are per-TEAM objects in Linear, not global
        strings, so this resolves *key*'s own team's states and matches by
        name before mutating. Raises ``store.MaestroError`` -- never a raw
        traceback -- if *key* isn't found or no state on its team matches
        *status*; ``push_phase_status`` below is the caller that catches this
        and degrades soft, so a reconcile is never wedged by a Linear-side
        error."""
        transport = self._transport_or_build()
        info = transport.get_workflow_states(key)
        issue_id = info.get("issue_id")
        if not issue_id:
            raise store.MaestroError(f"Linear: issue {key!r} not found")
        match = next((s for s in info.get("states") or [] if s.get("name") == status), None)
        if match is None:
            raise store.MaestroError(f"Linear: no workflow state named {status!r} on {key}'s team")
        if not transport.update_issue_state(issue_id, match["id"]):
            raise store.MaestroError(f"Linear: issueUpdate did not report success for {key} -> {status!r}")

    def push_phase_status(self, home: Path, key: str, external_id: str, phase: Phase, *,
                           actor: str = "reconciler") -> int:
        """T-104: push *phase*'s mapped Linear status for *external_id*
        (``STATUS_BY_PHASE``), idempotently. No-ops -- zero Linear calls --
        when *phase* has no mapped status, or the mapped status already
        matches the last one this ticket successfully pushed: the same
        "compare against the last recorded value" dedupe shape ``refresh``
        uses for ``updatedAt``, so re-entering the same phase (or a different
        phase mapped to the same status, e.g. ``awaiting-ci``/``in-review``
        both -> "In Review") mutates nothing. A failed push degrades soft: it
        records a ``Note`` and returns instead of raising -- callers
        (``ops.set_phase``/``ops.finalize``) must never be wedged by a
        Linear-side error. Returns the number of events appended."""
        status = STATUS_BY_PHASE[phase]
        if status is None:
            return 0
        if _last_pushed_status(home, key) == status:
            return 0
        try:
            self.transition(external_id, status)
        except Exception as e:
            event_log.append(
                home, key, E.NOTE,
                {"text": f"Linear status push to {status!r} failed: {e}"},
                actor=actor, step_id=f"linear-status-fail-{key}-{phase.value}-{status}")
            return 1
        event_log.append(
            home, key, E.LINEAR_STATUS_PUSHED,
            {"phase": phase.value, "status": status},
            actor=actor, step_id=f"linear-status-{key}-{phase.value}-{status}")
        return 1

    def assignee(self, key: str) -> str | None:
        data = self.view(key)
        try:
            return data["assignee"]["name"]
        except (KeyError, TypeError):
            return None

    # --- Import: Linear -> maestro `_new` inbox ----------------------------
    def import_new(self, home: Path) -> int:
        transport = self._transport_or_build()
        issues = transport.search_issues(self.import_filter)
        enqueued = 0
        for issue in issues:
            identifier = issue.get("identifier")
            if not identifier:
                continue
            maestro_key = f"LINEAR-{identifier}"
            if store.spec_path(home, maestro_key).exists():
                continue  # dedup guard: already imported
            title = issue.get("title") or identifier
            intent = issue.get("description") or title
            inbox.append_new(
                home, title, key=maestro_key,
                args={
                    "intent": intent,
                    "kind": "implementation",
                    "external_source": "linear",
                    "external_id": identifier,
                },
            )
            enqueued += 1
        return enqueued

    # --- Refresh: Linear -> event log for one tracked, not-done ticket -----
    def refresh(self, home: Path, key: str, external_id: str) -> int:
        transport = self._transport_or_build()
        data = transport.get_issue(external_id)
        updated_ts = data.get("updatedAt")
        if not updated_ts:
            return 0

        last_synced = _last_synced(home, key)
        if last_synced and last_synced.get("linear_updated_ts") == updated_ts:
            return 0  # nothing changed since the last sync

        comments = transport.get_comments(external_id)
        last_comment_id = comments[-1].get("id") if comments else None
        status = (data.get("state") or {}).get("name")

        appended = 0
        step_id = f"linear-sync-{key}-{updated_ts}"
        event_log.append(
            home, key, E.LINEAR_SYNCED,
            {"linear_updated_ts": updated_ts, "status": status, "last_comment_id": last_comment_id},
            actor="linear", step_id=step_id,
        )
        appended += 1

        if last_synced and last_synced.get("last_comment_id") != last_comment_id and last_comment_id:
            event_log.append(
                home, key, E.NOTE,
                {"text": f"New Linear comment on {external_id} (id {last_comment_id})"},
                actor="linear", step_id=f"linear-comment-{key}-{last_comment_id}",
            )
            appended += 1
        elif last_synced and last_synced.get("linear_updated_ts") != updated_ts:
            event_log.append(
                home, key, E.NOTE,
                {"text": f"{external_id} updated in Linear (status: {status})"},
                actor="linear", step_id=f"linear-note-{key}-{updated_ts}",
            )
            appended += 1

        return appended


def _last_synced(home: Path, key: str) -> dict | None:
    last = None
    for ev in event_log.read(home, key):
        if ev.get("type") == E.LINEAR_SYNCED:
            last = ev.get("payload") or {}
    return last


def _last_pushed_status(home: Path, key: str) -> str | None:
    """The status of the most recent ``LinearStatusPushed`` event, or
    ``None`` if this ticket has never had a status successfully pushed --
    the idempotency check ``push_phase_status`` compares its target against."""
    last = None
    for ev in event_log.read(home, key):
        if ev.get("type") == E.LINEAR_STATUS_PUSHED:
            last = (ev.get("payload") or {}).get("status")
    return last
