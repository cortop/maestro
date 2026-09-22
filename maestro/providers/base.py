"""Abstract provider interfaces. Concrete adapters live alongside in ``cli.py`` (or
a project's own module). The maestro core depends ONLY on these shapes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Tracker(Protocol):
    """An issue tracker (Jira, Linear, GitHub Issues, ...).

    ``import_new``/``refresh`` are the opt-in external-sync half (see
    ``dispatcher.sync_external_sources``) — a tracker with nothing to sync can
    leave them as no-ops (see ``NullTracker``).
    """

    def view(self, key: str) -> dict: ...
    def transition(self, key: str, status: str) -> None: ...
    def assignee(self, key: str) -> str | None: ...

    def import_new(self, home: Path) -> int:
        """Pull new external work into the ``_new`` inbox; return how many
        create-requests were enqueued."""
        ...

    def refresh(self, home: Path, key: str, external_id: str) -> int:
        """Refresh one tracked, not-done ticket from the external source;
        return how many events were appended (0 if nothing changed)."""
        ...


class VCS(Protocol):
    """A code host (GitHub, GitLab, ...). PR numbers are only unique WITHIN a repo,
    so every call takes an optional ``repo`` slug (e.g. "owner/repo"); ``None`` means
    "use the provider's own default" (today's single-repo/iterate-all behavior), so
    single-repo boards are unaffected. Callers resolve the slug via ``repos.py``.
    """

    def pr_for_branch(self, branch: str, repo: str | None = None,
                      env: dict | None = None) -> dict | None: ...

    def pr_status(self, pr_number: int, repo: str | None = None,
                  env: dict | None = None) -> dict:
        """Poll one PR's merge state and CI checks in a single round-trip.

        Returns {"state": "OPEN"|"MERGED"|"CLOSED"|"unknown",
        "mergeable": "MERGEABLE"|"CONFLICTING"|"UNKNOWN", "head_sha": str|None,
        "ci_state": "passing"|"failing"|"pending"|"unknown",
        "failing_checks": [str, ...], "draft": bool|None,
        "error": "auth"|"not_found"|"transient"|"unknown" (optional)}.

        ``error`` is a NEW field, absent (or ``None``) on a successful poll --
        callers must read it with ``.get("error")`` so implementations/fakes that
        predate it keep behaving as "no error". When the poll itself couldn't be
        completed (the underlying `gh`/host-CLI call failed), ``ci_state`` still
        collapses to its existing four values -- it is never overloaded -- and
        ``error`` carries WHY: "auth" (bad/expired credentials, SSO not granted),
        "not_found" (repo or PR doesn't resolve), "transient" (timeout/network --
        retry, don't spend failure budget), or "unknown" (unrecognized failure --
        today's behavior, unchanged).

        ``draft`` (T-86) is the freshly observed ``isDraft`` state, so
        ``dispatcher.sync_vcs`` can keep ``snap.pr_draft`` in sync rather than
        frozen at the implementer's original ``PrOpened`` append. ``None`` on an
        error poll (state unknown), or for an implementation/fake that predates
        this field -- callers must read it with ``.get("draft")``.

        ``env`` (GA-17) is the credential overlay to run the underlying `gh`
        call under (``dispatcher.resolve_credential``); None means the
        ambient environment, unchanged from before this ticket.
        """
        ...

    def review_feedback(self, pr_number: int, repo: str | None = None,
                        env: dict | None = None) -> list[dict]:
        """Return every review left on the PR as [{"id": str, "state": str|None,
        "body": str, "author": str|None}, ...]. The caller de-dupes per comment-id
        via the event log's step-id idempotency (see ``dispatcher.sync_vcs``).
        ``env`` (GA-17): see ``pr_status``."""
        ...

    def pr_ready(self, pr_number: int, repo: str | None = None,
                env: dict | None = None) -> dict:
        """Undraft a PR (``gh pr ready``, T-86). Returns ``{"ok": True}`` on
        success, or ``{"ok": False, "error": "auth"|"not_found"|"transient"|
        "unknown"}`` on a non-zero exit, classified the same way as
        ``pr_status``'s ``error`` field. The caller (``dispatcher._maybe_undraft``)
        never wedges the ticket on a failure here -- it records it and retries
        on a later sweep. ``env`` (GA-17): see ``pr_status``."""
        ...

    def rerun_failed(self, pr_number: int, head_sha: str, repo: str | None = None,
                     env: dict | None = None) -> dict:
        """T-123: request one ``gh run rerun --failed`` for *head_sha*'s
        currently-failing checks, having resolved their run ids from each
        failing check's own ``statusCheckRollup.detailsUrl`` (NOT ``gh run
        list --branch``, which takes a branch name rather than a SHA and can
        pick an older head's run on a fast-moving branch).

        Returns ``{"ok": True, "run_ids": [str, ...]}`` when at least one
        rerun request succeeded, or ``{"ok": False}`` when no run id could be
        resolved, the head SHA has since moved, or every rerun request
        failed. The caller (``dispatcher._observe_ci``) only records a
        ``CiRerunRequested`` -- and so only withholds routing -- when ``ok``
        is True; a False result falls straight through to routing exactly as
        if ``ci_auto_rerun`` were unset, so a resolution failure can never
        silently wedge a ticket in the grace window forever. ``env`` (GA-17):
        see ``pr_status``.
        """
        ...

    def failed_log_tail(self, run_id: str, max_bytes: int = 2048, repo: str | None = None,
                        env: dict | None = None) -> str:
        """T-123: the tail (<= *max_bytes*) of *run_id*'s failed job log
        (``gh run view <run_id> --log-failed``), for ``CiObserved.payload.
        failure_excerpt`` when ``ci_failure_excerpt = true``. Empty string on
        any failure to fetch -- never raises. ``env`` (GA-17): see
        ``pr_status``.
        """
        ...


class Fetcher(Protocol):
    """Imports external work into maestro by writing to the ``_new`` inbox."""

    def fetch(self) -> int:
        """Run the import; return how many create-requests were enqueued."""


# --- Null implementations: the default, fully project-agnostic behaviour --------
class NullTracker:
    def view(self, key: str) -> dict: return {}
    def transition(self, key: str, status: str) -> None: pass
    def assignee(self, key: str) -> str | None: return None
    def import_new(self, home: Path) -> int: return 0
    def refresh(self, home: Path, key: str, external_id: str) -> int: return 0


class NullVCS:
    def pr_for_branch(self, branch: str, repo: str | None = None,
                      env: dict | None = None) -> dict | None: return None
    def pr_status(self, pr_number: int, repo: str | None = None,
                  env: dict | None = None) -> dict:
        return {"state": "unknown", "mergeable": "UNKNOWN", "head_sha": None,
                "ci_state": "unknown", "failing_checks": [], "draft": None}
    def review_feedback(self, pr_number: int, repo: str | None = None,
                        env: dict | None = None) -> list[dict]: return []
    def pr_ready(self, pr_number: int, repo: str | None = None,
                env: dict | None = None) -> dict:
        return {"ok": False, "error": "unknown"}
    def rerun_failed(self, pr_number: int, head_sha: str, repo: str | None = None,
                     env: dict | None = None) -> dict:
        return {"ok": False}
    def failed_log_tail(self, run_id: str, max_bytes: int = 2048, repo: str | None = None,
                        env: dict | None = None) -> str:
        return ""


class NullFetcher:
    def fetch(self) -> int: return 0
