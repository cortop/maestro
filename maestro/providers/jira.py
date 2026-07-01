"""Jira Cloud REST adapter — opt-in ticket source (``tracker = "jira"``).

Talks to Jira over ``urllib.request`` only (stdlib, no third-party dep — the core's
dep-free constraint extends to providers here since REST beats a second CLI dep).
Two directions, both funneled through the audited channels the rest of maestro
already uses: ``import_new`` enqueues via ``inbox.append_new``; ``refresh`` appends
``JiraSynced``/``Note`` events for a tracked, not-done ticket.

Uses ``POST /rest/api/3/search/jql`` (the only supported search endpoint on Cloud —
the old ``/search`` was removed in 2025) and treats descriptions/comments as ADF
(Atlassian Document Format) JSON, never plain text.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from .. import event_log, events as E, inbox, store


class JiraTransport(Protocol):
    """The external HTTP boundary — the only thing a test should fake."""

    def search_jql(self, jql: str, fields: list[str]) -> list[dict]: ...
    def get_issue(self, key: str, fields: list[str]) -> dict: ...
    def get_comments(self, key: str) -> list[dict]: ...


class HttpJiraTransport:
    """Real transport: HTTP Basic auth (email:token), JSON over urllib."""

    def __init__(self, base_url: str, email: str, token: str):
        self.base_url = base_url.rstrip("/")
        auth = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers, method=method)
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 4:
                    retry_after = int(e.headers.get("Retry-After", "5"))
                    time.sleep(retry_after)
                    continue
                raise

    def search_jql(self, jql: str, fields: list[str]) -> list[dict]:
        issues: list[dict] = []
        body: dict = {"jql": jql, "fields": fields, "maxResults": 100}
        while True:
            data = self._request("POST", "/rest/api/3/search/jql", body)
            issues.extend(data.get("issues", []))
            token = data.get("nextPageToken")
            if not token:
                break
            body["nextPageToken"] = token
        return issues

    def get_issue(self, key: str, fields: list[str]) -> dict:
        return self._request("GET", f"/rest/api/3/issue/{key}?fields={','.join(fields)}")

    def get_comments(self, key: str) -> list[dict]:
        data = self._request("GET", f"/rest/api/3/issue/{key}/comment")
        return data.get("comments", [])


def adf_to_text(node) -> str:
    """Flatten an Atlassian Document Format node tree to plain text."""
    if not isinstance(node, dict):
        return ""
    parts = []
    if node.get("type") == "text":
        parts.append(node.get("text", ""))
    for child in node.get("content") or []:
        parts.append(adf_to_text(child))
    sep = "\n" if node.get("type") in {"paragraph", "heading"} else ""
    return sep.join(p for p in parts if p) + (sep if sep and parts else "")


class JiraTracker:
    """Implements the ``Tracker`` protocol plus the import/refresh sync half."""

    def __init__(self, settings: dict, transport: JiraTransport | None = None):
        self.settings = settings
        self.import_jql = settings.get(
            "import_jql",
            "(reporter = currentUser() OR assignee = currentUser()) AND statusCategory = 'To Do'",
        )
        self.import_fields = settings.get("import_fields", ["summary", "description", "status", "issuetype"])
        self.sync_interval = int(settings.get("sync_interval", 900))
        self._transport = transport

    def _transport_or_build(self) -> JiraTransport:
        if self._transport is not None:
            return self._transport
        base_url = self.settings.get("base_url", "")
        email = self.settings.get("email", "")
        token_env = self.settings.get("token_env", "JIRA_API_TOKEN")
        import os
        token = os.environ.get(token_env, "")
        self._transport = HttpJiraTransport(base_url, email, token)
        return self._transport

    # --- Tracker protocol -------------------------------------------------
    def view(self, key: str) -> dict:
        return self._transport_or_build().get_issue(key, self.import_fields)

    def transition(self, key: str, status: str) -> None:
        pass  # not needed by the sync flow; status changes flow the other way (Jira -> maestro)

    def assignee(self, key: str) -> str | None:
        data = self.view(key)
        try:
            return data["fields"]["assignee"]["displayName"]
        except (KeyError, TypeError):
            return None

    # --- Import: Jira -> maestro `_new` inbox -----------------------------
    def import_new(self, home: Path) -> int:
        transport = self._transport_or_build()
        issues = transport.search_jql(self.import_jql, self.import_fields)
        enqueued = 0
        for issue in issues:
            jira_key = issue.get("key")
            if not jira_key:
                continue
            maestro_key = f"JIRA-{jira_key}"
            if store.spec_path(home, maestro_key).exists():
                continue  # dedup guard: already imported
            fields = issue.get("fields") or {}
            title = fields.get("summary") or jira_key
            intent = adf_to_text(fields.get("description")) or title
            inbox.append_new(
                home, title, key=maestro_key,
                args={
                    "intent": intent,
                    "kind": "implementation",
                    "external_source": "jira",
                    "external_id": jira_key,
                },
            )
            enqueued += 1
        return enqueued

    # --- Refresh: Jira -> event log for one tracked, not-done ticket ------
    def refresh(self, home: Path, key: str, external_id: str) -> int:
        transport = self._transport_or_build()
        fields = ["status", "summary", "description", "updated"]
        data = transport.get_issue(external_id, fields)
        issue_fields = data.get("fields") or {}
        updated_ts = issue_fields.get("updated")
        if not updated_ts:
            return 0

        last_synced = _last_synced(home, key)
        if last_synced and last_synced.get("jira_updated_ts") == updated_ts:
            return 0  # nothing changed since the last sync

        comments = transport.get_comments(external_id)
        last_comment_id = comments[-1].get("id") if comments else None
        status = (issue_fields.get("status") or {}).get("name")

        appended = 0
        step_id = f"jira-sync-{key}-{updated_ts}"
        event_log.append(
            home, key, E.JIRA_SYNCED,
            {"jira_updated_ts": updated_ts, "status": status, "last_comment_id": last_comment_id},
            actor="jira", step_id=step_id,
        )
        appended += 1

        if last_synced and last_synced.get("last_comment_id") != last_comment_id and last_comment_id:
            event_log.append(
                home, key, E.NOTE,
                {"text": f"New Jira comment on {external_id} (id {last_comment_id})"},
                actor="jira", step_id=f"jira-comment-{key}-{last_comment_id}",
            )
            appended += 1
        elif last_synced and last_synced.get("jira_updated_ts") != updated_ts:
            event_log.append(
                home, key, E.NOTE,
                {"text": f"{external_id} updated in Jira (status: {status})"},
                actor="jira", step_id=f"jira-note-{key}-{updated_ts}",
            )
            appended += 1

        return appended


def _last_synced(home: Path, key: str) -> dict | None:
    last = None
    for ev in event_log.read(home, key):
        if ev.get("type") == E.JIRA_SYNCED:
            last = ev.get("payload") or {}
    return last
