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
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from .. import event_log, events as E, inbox, store


class LinearTransport(Protocol):
    """The external HTTP boundary — the only thing a test should fake."""

    def search_issues(self, filter: dict) -> list[dict]: ...
    def get_issue(self, identifier: str) -> dict: ...
    def get_comments(self, identifier: str) -> list[dict]: ...


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
        pass  # not needed by the sync flow; status changes flow the other way (Linear -> maestro)

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
