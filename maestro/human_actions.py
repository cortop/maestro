"""Human actions (answer, message, retry, discard, hold, release, stop, pause, resume, create-pending-review) shared by the TUI, CLI and web UI."""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from . import dispatcher as disp
from . import events as E
from . import fleet, inbox, ops, store
from . import snapshot as snap_mod
from .config import Config
from .statemachine import Phase

_PR_URL = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")

# Called with the ticket key (or None) after a successful inbox write; None = no nudge.
Nudge = Callable[[str | None], object]


class StopRefused(store.MaestroError):
    """`stop` could not stop the session; ``result`` is the `ops.stop_session` dict."""

    def __init__(self, result: dict):
        super().__init__(str(result.get("outcome") or "session not stopped"))
        self.result = result


def _fire(nudge: Nudge | None, key: str | None) -> None:
    if nudge is not None:
        nudge(key)


def _require_minted(home: Path, key: str) -> None:
    if disp._never_minted(home, key):
        raise store.MaestroError(f"unknown ticket {key!r}")


def answer(home: Path, key: str, text: str, *, qid: str | None = None,
           accepted_recommendation: bool = False, nudge: Nudge | None = None) -> dict:
    """Queue an `ans` for *key*; ``accepted_recommendation`` marks a recommendation accept."""
    args: dict = {"text": text}
    if qid:
        args["qid"] = qid
    if accepted_recommendation:
        args["accepted_recommendation"] = True
    entry = inbox.append_command(home, key, "ans", args)
    _fire(nudge, key)
    return entry


def message(home: Path, key: str, text: str, *, nudge: Nudge | None = None) -> dict:
    """Queue a free-text `msg` for *key*'s agent."""
    entry = inbox.append_command(home, key, "msg", {"text": text})
    _fire(nudge, key)
    return entry


def _degraded_only(home: Path, key: str, command: str, nudge: Nudge | None) -> dict:
    if snap_mod.load(home, key).phase != Phase.DEGRADED.value:
        raise store.MaestroError(f"{key}: '{command}' only applies to degraded tickets")
    entry = inbox.append_command(home, key, command, {})
    _fire(nudge, key)
    return entry


def retry(home: Path, key: str, *, nudge: Nudge | None = None) -> dict:
    """Queue `retry` for a degraded ticket; refuses any other phase."""
    return _degraded_only(home, key, "retry", nudge)


def discard(home: Path, key: str, *, nudge: Nudge | None = None) -> dict:
    """Queue `discard` for a degraded ticket; refuses any other phase."""
    return _degraded_only(home, key, "discard", nudge)


def hold(home: Path, key: str, *, until: float | None = None, reason: str | None = None) -> dict:
    """Hold *key* so the dispatcher never spawns it; refuses an unknown ticket."""
    _require_minted(home, key)
    return fleet.hold(home, key, until=until, reason=reason)


def release(home: Path, key: str) -> dict:
    """Release *key*'s hold; refuses an unknown ticket with no hold file."""
    if not fleet.hold_path(home, key).exists():
        _require_minted(home, key)
    return fleet.unhold(home, key)


def stop(cfg: Config, key: str, *, wait: float = 10.0) -> dict:
    """SIGTERM *key*'s live session; raises `StopRefused` unless the process stopped."""
    result = ops.stop_session(cfg, key, wait=wait)
    if not result["stopped"]:
        raise StopRefused(result)
    return result


def pause(home: Path, *, until: float | None = None, reason: str | None = None) -> dict:
    """Pause the whole fleet."""
    return fleet.pause(home, until=until, reason=reason)


def resume(home: Path) -> dict:
    """Resume a paused fleet."""
    return fleet.resume(home)


def create_pending_review(cfg: Config, key: str, findings: list[dict], *, vcs=None) -> dict:
    """Create ONE pending (never submitted) GitHub review from a review ticket's kept findings.

    *findings* is ``[{"index": int, "comment": str}, ...]``: the round's finding at ``index``
    (file and line come from the folded snapshot) with the human's edited comment, posted
    verbatim. A finding whose line is not in the PR's diff goes in the review body with its
    file and line. Plain Python through the VCS provider and the repo's credential: no agent
    session runs between the click and GitHub. Refuses, creating nothing, when the PR has
    new commits, the user already has a pending review on it, or nothing was kept. Afterwards
    the ticket sleeps in `awaiting-ci` and `dispatcher.sync_review_pending` finishes it."""
    snap = snap_mod.load(cfg.home, key)
    if snap.kind != "review":
        raise store.MaestroError(f"{key}: only a review ticket can create a pending review")
    if snap.phase != Phase.AWAITING_HUMAN.value or not snap.review_brief or not snap.review_head_sha:
        raise store.MaestroError(f"{key}: the review brief is not ready (phase {snap.phase})")
    m = _PR_URL.match(snap.review_url or "")
    if not m:
        raise store.MaestroError(f"{key}: no GitHub pull request url on this review")
    slug, number = m.group(1), int(m.group(2))
    chosen: list[tuple[dict, str]] = []
    seen: set[int] = set()
    for f in findings:
        i = f.get("index")
        if not isinstance(i, int) or not 0 <= i < len(snap.review_comments) or i in seen:
            raise store.MaestroError(f"{key}: unknown or repeated finding {i!r}")
        seen.add(i)
        text = f.get("comment")
        if not isinstance(text, str) or not text.strip():
            raise store.MaestroError(f"{key}: finding {i + 1} has an empty comment")
        chosen.append((snap.review_comments[i], text))
    if not chosen:
        raise store.MaestroError(f"{key}: no finding is included; nothing to send")

    from . import providers
    from . import repos as repos_mod
    cred = disp.resolve_credential(repos_mod.resolve(cfg, cfg.home, key), {})
    if not cred.ok:
        raise store.MaestroError(f"{key}: credential resolution failed ({cred.error})")
    vcs = vcs or providers.get_vcs(cfg)

    def need(res: dict, what: str) -> dict:
        if res.get("ok") is False or res.get("error"):
            detail = " ".join((res.get("detail") or "").split())[:300]
            why = f"{res.get('error')}: {detail}" if detail else res.get("error")
            raise store.MaestroError(f"{key}: could not {what} on GitHub ({why})")
        return res

    head = need(vcs.pr_head(number, slug, env=cred.env), "read the pull request")
    if head.get("head_sha") != snap.review_head_sha:
        raise store.MaestroError(
            f"{key}: the pull request has new commits since this brief; start a new review round first")
    if head.get("state") not in (None, "OPEN"):
        raise store.MaestroError(f"{key}: the pull request is {head.get('state')}, not open")
    existing = need(vcs.pending_review(number, slug, env=cred.env), "look for a pending review")
    if existing.get("review_id"):
        raise store.MaestroError(
            f"{key}: you already have a pending review on this pull request; submit or delete it "
            f"on GitHub first. Nothing was created.")
    in_diff = need(vcs.pr_diff_lines(number, slug, env=cred.env), "read the diff")["lines"]

    inline, in_body = [], []
    for f, text in chosen:
        if f["line"] in in_diff.get(f["file"], ()):
            inline.append({"path": f["file"], "line": f["line"], "body": text})
        else:
            in_body.append(f"`{f['file']}:{f['line']}`\n\n{text}")
    body = "\n\n".join(in_body)
    made = need(vcs.create_pending_review(number, snap.review_head_sha, body, inline, slug,
                                          env=cred.env), "create the pending review")

    payload = {"review_id": made["id"], "pr_number": number, "head_sha": snap.review_head_sha,
               "inline": len(inline), "in_body": len(in_body)}
    ops._append(cfg, key, E.REVIEW_PENDING_CREATED, payload, actor="human",
                sid=f"review-pending-{key}-{snap.review_head_sha}")
    for qid in snap.open_questions:
        ops._append(cfg, key, E.QUESTION_ANSWERED,
                    {"qid": qid, "answer": "pending review created on GitHub"},
                    actor="human", sid=f"ans-{key}-pending-{qid}")
    ops.set_phase(cfg, key, Phase.AWAITING_CI, reason="pending review created; waiting for submit",
                  actor="human")
    return payload


def queue_ticket(cfg: Config, title: str, *, priority: int = 3, nudge: Nudge | None = None) -> dict:
    """Queue a create-ticket request exactly as `maestro create <title> --priority N`; refuses an empty title or a non-board home."""
    title = (title or "").strip()
    if not title:
        raise store.MaestroError("title must not be empty")
    board = store.board_state(cfg.home)
    if board["state"] in ("missing", "uninitialized", "partial"):
        raise store.MaestroError(f"{cfg.home} is not a maestro board ({board['state']}); run `maestro init` first")
    entry = inbox.append_new(cfg.home, title, args={"priority": int(priority)})
    _fire(nudge, None)
    return entry


def start_review(cfg: Config, pr_url: str, *, source: str | None = None, vcs=None) -> dict:
    """Start (or advance) a review of someone else's PR; raises on a malformed URL, returns ``ops.start_review``'s result."""
    return ops.start_review(cfg, pr_url, source=source, vcs=vcs)


def import_linear(cfg: Config, url_or_id: str, *, tracker=None) -> dict:
    """Mint a ticket from a Linear URL or identifier; ``minted`` is False for an already-imported key."""
    return ops.import_linear(cfg, url_or_id, tracker=tracker)
