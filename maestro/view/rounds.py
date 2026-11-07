"""Round view-models: the live round label and a ticket's per-round history from events only."""
from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import config as config_mod, event_log, implementing_brief, outcomes, repos, snapshot as snap_mod, store
from .. import events as E
from ..sessions import list_sessions
from . import events as ev_view

_PHASE_LABELS = {
    "triaging": "Triaging",
    "researching": "Researching",
    "ready": "Setting up",
    "awaiting-human": "Applying your answer",
}


def _feedback_window_start(events: list[dict]) -> int:
    """Seq of the phase change before the latest one into implementing: review comments
    arrive while the ticket waits in review, so the window opens there."""
    changes = [ev["seq"] for ev in events if ev["type"] == E.PHASE_CHANGED]
    return changes[-2] if len(changes) > 1 else 0


def _implementing_label(home: Path, key: str, events: list[dict]) -> str:
    name, _reason = implementing_brief.current_round(home, key)
    if name == "review-feedback":
        try:
            cfg = config_mod.load(str(home))
        except store.MaestroError:
            cfg = None
        start = _feedback_window_start(events)
        n = sum(1 for ev in events if ev["type"] == E.REVIEW_FEEDBACK_RECEIVED and ev["seq"] > start
                and not outcomes.is_bot(cfg, ev.get("payload", {}).get("author")))
        return f"Addressing review feedback · {n} comment{'' if n == 1 else 's'}"
    if name == "qa-fix":
        fails = [ev for ev in events if ev["type"] == E.AC_QA_VERDICT
                 and ev.get("payload", {}).get("verdict") == "fail"]
        return f"Fixing QA failure · AC {fails[-1]['payload'].get('ac_index')}" if fails else "Fixing QA failure"
    if name == "ci-fix":
        cis = [ev for ev in events if ev["type"] == E.CI_OBSERVED]
        failing = (cis[-1].get("payload", {}).get("failing_checks") or []) if cis else []
        return f"Fixing CI · {failing[0]}" if failing else "Fixing CI"
    return {
        "conflict": "Resolving merge conflict",
        "base-drift": "Syncing with base",
        "split-decision": "Splitting the PR",
        "fresh": "Implementing the ticket",
    }.get(name, "Implementing (your message)")


def round_label(home: Path, key: str) -> str:
    """One line naming the round the ticket's session works on and its trigger; empty for a
    phase no agent session handles."""
    snap = snap_mod.load(home, key)
    events = event_log.read(home, key)
    if events and events[-1]["type"] == E.POST_QA_SKILL_SPAWNED:
        return "Polishing the PR"
    if snap.phase == "implementing":
        return _implementing_label(home, key, events)
    if snap.phase == "qa":
        spec = store.spec_path(home, key)
        n = len(snap_mod.parse_acs(spec.read_text(encoding="utf-8"))) if spec.exists() else 0
        return f"QA · checking {n} AC{'' if n == 1 else 's'}"
    return _PHASE_LABELS.get(snap.phase, "")


# --- review threads ----------------------------------------------------------

_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")


def review_threads(events: list[dict]) -> list[str]:
    """One text block per ReviewFeedbackReceived: author, state, location, body and the
    replies posted for its comment id. Markdown images keep their alt text only."""
    replies: dict[str, list[dict]] = {}
    for e in events:
        if e["type"] == E.REVIEW_REPLY_POSTED:
            replies.setdefault(e["payload"].get("comment_id"), []).append(e["payload"])
    blocks = []
    for e in events:
        if e["type"] != E.REVIEW_FEEDBACK_RECEIVED:
            continue
        p = e["payload"]
        where = f" {p['path']}:{p['line']}" if p.get("path") and p.get("line") else \
            (f" {p['path']}" if p.get("path") else "")
        body = _MD_IMAGE.sub(r"\1", (p.get("body") or "")).strip()
        lines = [f"{p.get('author', '?')} [{p.get('state', '?')}]{where}", body]
        for r in replies.get(p.get("comment_id"), []):
            lines.append("  ↳ reply: " + _MD_IMAGE.sub(r"\1", (r.get("body") or "")).strip())
        blocks.append("\n".join(lines))
    return blocks


# --- per-ticket round history -------------------------------------------------

_ROW_LABELS = {"triaging": "Triage", "researching": "Research", "ready": "Setup",
               "qa": "QA", "awaiting-human": "asked you"}
_CI_PHASES = frozenset({"awaiting-ci", "in-review", "verifying"})
_BARE_REASON = re.compile(r"^[\w-]+ round done$")


@dataclass(frozen=True)
class Round:
    when: str          # "MM-DD HH:MM" (UTC)
    label: str
    duration: str      # agent busy time, "" when unknown
    outcome: str
    warn: bool = False
    count: int = 1
    first_session: str = ""  # path of the earliest session log in the round, "" when none


@dataclass(frozen=True)
class TicketRounds:
    key: str
    phase: str
    title: str
    time: str
    rounds: str
    qa: str
    review: str
    pr: str
    trouble: str
    rows: tuple[Round, ...]


def _dur(seconds: float) -> str:
    return ev_view.fmt_duration(seconds)


def _when(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ts))


def _quote_reason(reason: str) -> str:
    reason = " ".join((reason or "").split())
    if not reason or _BARE_REASON.match(reason):
        return ""
    if ": " in reason and _BARE_REASON.match(reason.split(": ", 1)[0]):
        reason = reason.split(": ", 1)[1]
    return f'"{reason}"'


def _join(*parts: str) -> str:
    return " · ".join(p for p in parts if p)


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def _qa_outcome(window: list[dict]) -> tuple[str, str]:
    """(token, outcome): token is `✗ AC i` / `✓ n/n` / ""; outcome adds evidence and note."""
    latest: dict[str, dict] = {}
    notes = []
    for e in window:
        if e["type"] == E.AC_QA_VERDICT:
            latest[e["payload"].get("ac_hash")] = e["payload"]
        elif e["type"] == E.NOTE:
            notes.append(_first_line(e["payload"].get("text", "")))
    fails = [v for v in latest.values() if v.get("verdict") == "fail"]
    token, text = "", ""
    if fails:
        token = f"✗ AC{fails[0].get('ac_index')}"
        text = f"✗ AC {fails[0].get('ac_index')}: {_first_line(fails[0].get('evidence') or '')}"
    elif latest:
        token = text = f"✓ {len(latest)}/{len(latest)}"
    note = next((n for n in notes if n), "")
    return token, _join(text, f"note: {note}" if note else "")


def _sessions_in(sessions: list[dict], start: float, end: float) -> tuple[int, float]:
    n, busy = 0, 0.0
    for s in sessions:
        if start <= s["epoch"] < end:
            n += 1
            try:
                busy += max(0.0, os.stat(s["path"]).st_mtime - s["epoch"])
            except OSError:
                pass
    return n, busy


def _collapse(rows: list[Round]) -> list[Round]:
    out: list[Round] = []
    for r in rows:
        if out and not r.warn and not out[-1].warn and (out[-1].label, out[-1].outcome) == (r.label, r.outcome):
            out[-1] = Round(out[-1].when, out[-1].label, out[-1].duration, out[-1].outcome,
                            False, out[-1].count + 1, out[-1].first_session or r.first_session)
        else:
            out.append(r)
    return out


def _review_skill(cfg, home: Path, key: str) -> str:
    if cfg is None:
        return "unknown (config unreadable)"
    from .. import dispatcher
    skill = repos.resolve(cfg, home, key).qa_skill
    runner = dispatcher.resolve_runner(cfg, key, "qa")[0]
    if not skill:
        return "skill NOT RUN: qa_skill not configured for this repo (current config)"
    if runner != "claude":
        return f"skill NOT RUN: {skill} configured, but the qa runner {runner} cannot run skills (current config)"
    return f"skill {skill} (current config)"


def build(home: Path, key: str, now: float | None = None) -> TicketRounds:
    """Fold *key*'s events (plus stat-only session logs) into the header and round rows."""
    now = time.time() if now is None else now
    try:
        cfg = config_mod.load(str(home))
    except store.MaestroError:
        cfg = None
    snap = snap_mod.load(home, key)
    events = event_log.read(home, key)
    moves = ev_view._phase_moves(events)
    sessions = list_sessions(home, key)
    retention = (cfg.session_log_retention_days if cfg else None) or 0

    rows: list[tuple[float, Round]] = []
    dwell: dict[str, float] = {}
    qa_chain: list[str] = []
    for i, m in enumerate(moves):
        end_ts = moves[i + 1]["ts"] if i + 1 < len(moves) else now
        end_seq = moves[i + 1]["seq"] if i + 1 < len(moves) else float("inf")
        prev_seq = moves[i - 1]["seq"] if i else 0
        dwell[m["phase"]] = dwell.get(m["phase"], 0.0) + max(0.0, end_ts - m["ts"])
        phase = m["phase"]
        window = [e for e in events if (m["seq"] or 0) <= e["seq"] < end_seq]
        closing = moves[i + 1]["reason"] if i + 1 < len(moves) else ""
        if phase == "implementing":
            label = outcomes.implementing_round(m["reason"], moves[i - 1]["phase"] if i else None)
            outcome = _quote_reason(closing)
            if label == "review-feedback":
                span = [e for e in events if prev_seq < e["seq"] < end_seq]
                comments = [e for e in span if e["type"] == E.REVIEW_FEEDBACK_RECEIVED
                            and e["payload"].get("state") != "APPROVED"
                            and not outcomes.is_bot(cfg, e["payload"].get("author"))]
                nrep = sum(1 for e in span if e["type"] == E.REVIEW_REPLY_POSTED)
                outcome = _join(f"{len(comments)} comment{'' if len(comments) == 1 else 's'} → "
                                f"{nrep} repl{'y' if nrep == 1 else 'ies'}", outcome)
            if label == "qa-fix":
                qa_chain.append("qa-fix")
        elif phase == "qa":
            label = "QA"
            token, outcome = _qa_outcome(window)
            if token:
                qa_chain.append(token)
        elif phase == "awaiting-human":
            label = "asked you"
            asked = next((e for e in window if e["type"] == E.QUESTION_ASKED), None)
            answered = next((e for e in window if e["type"] == E.QUESTION_ANSWERED), None)
            if asked is None:
                continue
            a_ts = store.iso_to_epoch(asked["ts"]) or m["ts"]
            r_ts = store.iso_to_epoch(answered["ts"]) if answered else None
            outcome = _join(f'"{_first_line(asked["payload"].get("text", ""))}"',
                            f"answered after {_dur(r_ts - a_ts)}" if r_ts else "unanswered")
        elif phase in _ROW_LABELS:
            label, outcome = _ROW_LABELS[phase], _quote_reason(closing)
        else:
            continue
        duration = ""
        first_session = ""
        if phase != "awaiting-human":
            hi = end_ts if i + 1 < len(moves) else float("inf")
            inside = [x for x in sessions if m["ts"] <= x["epoch"] < hi]
            if inside:
                first_session = min(inside, key=lambda x: x["epoch"])["path"]
            n, busy = _sessions_in(sessions, m["ts"], end_ts if i + 1 < len(moves) else float("inf"))
            if n:
                duration = _dur(busy)
            elif retention and now - m["ts"] > retention * 86400:
                outcome = _join(outcome, "(session log pruned)")
        rows.append((m["ts"], Round(_when(m["ts"]), label, duration, outcome,
                                     first_session=first_session)))

    failed = stalled = 0
    for e in events:
        if e["type"] in (E.FAILED, E.STALLED):
            ts = store.iso_to_epoch(e.get("ts")) or 0.0
            p = e["payload"]
            if e["type"] == E.FAILED:
                failed += 1
                text = p.get("error") or ""
            else:
                stalled += 1
                text = p.get("reason") or ""
            rows.append((ts, Round(_when(ts), f"⚠ {e['type']}", "", _first_line(text), warn=True)))
    rows.sort(key=lambda t: t[0])
    final = _collapse([r for _, r in rows])

    asked_q = {e["payload"].get("qid") for e in events if e["type"] == E.QUESTION_ASKED}
    answered_q = {e["payload"].get("qid") for e in events if e["type"] == E.QUESTION_ANSWERED}
    unanswered = len(asked_q - answered_q)

    replied = {e["payload"].get("comment_id") for e in events if e["type"] == E.REVIEW_REPLY_POSTED}
    human = bot = unreplied = nreplied = 0
    for e in events:
        if e["type"] != E.REVIEW_FEEDBACK_RECEIVED:
            continue
        p = e["payload"]
        if p.get("state") == "APPROVED" or outcomes.is_bot(cfg, p.get("author")):
            bot += 1
            continue
        human += 1
        if p.get("comment_id") in replied:
            nreplied += 1
        else:
            unreplied += 1

    busy_total = _sessions_in(sessions, 0, float("inf"))[1]
    total = sum(dwell.values())
    time_line = " · ".join([
        f"{_dur(total)} total", f"agent busy {_dur(busy_total)}",
        f"CI/review {_dur(sum(v for k, v in dwell.items() if k in _CI_PHASES))}",
        f"waiting on you {_dur(dwell.get('awaiting-human', 0.0))}"])
    counts: dict[str, int] = {}
    for _, r in rows:
        if r.label in implementing_brief.ROUNDS or r.label == "unknown":
            counts[r.label] = counts.get(r.label, 0) + 1
    trouble = []
    if unanswered:
        trouble.append(f"{unanswered} question{'' if unanswered == 1 else 's'} unanswered")
    if stalled:
        trouble.append(f"{stalled} stalled")
    if failed:
        trouble.append(f"{failed} failure{'' if failed == 1 else 's'}")
    chain: list[str] = []
    for t in qa_chain:
        if not chain or chain[-1] != t:
            chain.append(t)
    return TicketRounds(
        key=key, phase=snap.phase, title=snap.title or "", time=time_line,
        rounds=" · ".join(f"{n} {label}" for label, n in counts.items()) or "none",
        qa=" → ".join(chain) or "no QA verdicts",
        review=_review_skill(cfg, home, key),
        pr=" · ".join([f"{human} comments", f"{nreplied} replied", f"{unreplied} without reply",
                       f"{bot} bot/system"]),
        trouble=" · ".join(trouble) or "none",
        rows=tuple(final))


def format_lines(tr: TicketRounds) -> list[str]:
    """The header and one line per row, shared by the CLI and the TUI."""
    lines = [f"{tr.key} · {tr.phase} · {tr.title}",
             f"Time     {tr.time}", f"Rounds   {tr.rounds}", f"QA       {tr.qa}",
             f"Review   {tr.review}", f"PR       {tr.pr}", f"Trouble  {tr.trouble}"]
    for r in tr.rows:
        label = f"{r.label} ×{r.count}" if r.count > 1 else r.label
        lines.append(f" {r.when}  {label:<18} {r.duration:>5}  {r.outcome}".rstrip())
    return lines


def to_json(tr: TicketRounds) -> dict:
    return asdict(tr)
