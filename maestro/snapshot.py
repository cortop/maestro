"""Snapshot = the folded projection of one ticket's event log.

Tiny (~1-2KB), disposable, machine-owned, DO-NOT-EDIT. The dispatcher reads only
snapshots to decide what is due, so a sweep never touches the full event history
or any of the old 100-500KB monoliths. Writers refresh the snapshot after every
append, so the dispatcher's cheap read is always current.

``fold`` is a TOTAL function of the log (RB-2, and RB-10's property-based
regression in ``tests/test_snapshot_properties.py``): no event, however
malformed its payload, may raise. A ``PhaseChanged`` with a missing/unrecognized
``phase``, or an ``ImplTurn``/``ImplStep`` with a non-integer ``turn``, is
coerced to a safe default (the phase is left unchanged; the turn counter is
left unchanged) and recorded in ``fold_warnings`` instead of being silently
dropped -- a corrupt log must stay visible, never crash the fold. A ``payload``
that isn't even a dict (a bare int/str/list) is likewise coerced to ``{}``
before any event-type arm touches it. ``observed_seq`` is a high-water mark
(``max`` across every event's ``seq``, not last-write) so an out-of-order log
segment can't move it backwards. ``DONE`` is absorbing: once folded to
``Phase.DONE``, NO later event of ANY type can move the phase again -- see
``fold``'s ``TICKET_CREATED``/``PHASE_CHANGED``/``STALLED`` arms (this is a
`fold` law, held for any event list `fold` is handed, not merely a
consequence of what the write boundary would actually append -- e.g.
``ops.mint_new_tickets`` already refuses a second ``TicketCreated`` on a key
with events, but `fold` still guards it). ``fold`` is
also duplicate-idempotent (``fold(evs) == fold(evs * 2)``): it dedups its
input by seq itself, on top of ``event_log.read`` deduping upstream (an
interrupted ``ops.compact`` can otherwise leave the same seq in both the
archive and the active log -- see that module's docstring).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import events as E
from . import event_log, store
from .idempotency import content_hash
from .statemachine import Phase

# Spec acceptance criteria are Markdown task-list items: "- [ ] ..." / "- [x] ...".
_AC_RE = re.compile(r"^- \[[ xX]\]\s*(.+)$", re.MULTILINE)

# A spec's title is its first level-1 heading, conventionally "# <KEY>: <title>".
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# T-79: an opt-in, machine-checkable annotation trailing an AC line --
# `(test: <path>)`, `(test: <path>::<id>)`, or `(check: <shell command>)`.
# Anchored at end-of-line and disallows embedded parens in the body (so a
# SECOND trailing parenthetical, e.g. "(test: a.py) (see #123)", is never
# swallowed into the annotation) -- any other trailing parenthetical (e.g.
# "(checked manually)") simply doesn't match and the AC stays plain text,
# exactly as before this ticket.
_AC_ANNOTATION_RE = re.compile(r"\((test|check):\s*([^()]+?)\)\s*$")


@dataclass(frozen=True)
class AcAnnotation:
    """A parsed `test:`/`check:` annotation off one AC line (T-79).

    `raw` is the annotation body verbatim. For `kind == "test"`, `path` is the
    test file and `test_id` is the optional `::<id>` suffix (a bare file path
    means "some test in that file, added by this diff, passes"). For
    `kind == "check"`, `command` is the shell command that must exit 0.
    """
    kind: str  # "test" | "check"
    raw: str
    path: str | None = None
    test_id: str | None = None
    command: str | None = None


def parse_ac_annotation(ac_text: str) -> AcAnnotation | None:
    """Parse a trailing `(test: ...)` / `(check: ...)` annotation off one AC's
    text, or ``None`` if the line carries no such annotation (or a different,
    ordinary trailing parenthetical) -- nothing downstream treats an AC as
    machine-checkable unless this returns non-``None``, which is what makes
    the feature ship dark by construction."""
    m = _AC_ANNOTATION_RE.search(ac_text)
    if not m:
        return None
    kind, body = m.group(1), m.group(2).strip()
    if not body:
        return None
    if kind == "check":
        return AcAnnotation(kind="check", raw=body, command=body)
    if "::" in body:
        path, test_id = (p.strip() for p in body.split("::", 1))
        if not path or not test_id:
            return None
        return AcAnnotation(kind="test", raw=body, path=path, test_id=test_id)
    return AcAnnotation(kind="test", raw=body, path=body)


def parse_acs(spec_text: str) -> list[str]:
    """Extract acceptance-criteria line texts (in spec order) from a spec's body.

    Byte-identical to before annotations existed (T-79): an annotation is just
    trailing text on the line, part of the same string this always returned --
    see `parse_ac_annotation` for pulling it back out."""
    return [m.group(1).strip() for m in _AC_RE.finditer(spec_text)]


def parse_title(spec_text: str, key: str | None = None) -> str | None:
    """The spec's first level-1 heading, with a leading ``<KEY>: `` stripped.

    The fallback for a ticket whose log carries no ``TicketCreated`` — which is a
    real, supported state, not only a mishap: ``dispatcher.list_keys`` discovers a
    ticket from a bare ``tickets/<KEY>/`` directory, and ``mint_new_tickets``
    deliberately declines to append a second ``TicketCreated`` to a key that
    already has events (it would clobber the folded phase back to triaging). Such
    a ticket has a perfectly good title in its own spec and ``None`` in its
    snapshot, so every dashboard rendered it blank.
    """
    m = _TITLE_RE.search(spec_text)
    if not m:
        return None
    title = m.group(1).strip()
    if key and title.startswith(f"{key}:"):
        title = title[len(key) + 1:].strip()
    return title or None


def display_title(home: Path, s: "Snapshot") -> str:
    """The title to show a human: the folded one, else the spec's own H1.

    Read from disk on demand, exactly like `gates.spec_priority` -- so a ticket
    with no `TicketCreated` still renders with a title, and a human's edit to
    the spec's heading takes effect on the next render rather than never. Lives
    here, below `projection`/`notify`/`tui`, so all three human-facing surfaces
    share ONE definition instead of each re-deriving the fallback. Total: a
    missing or unreadable spec falls back to the empty string.
    """
    if s.title:
        return s.title
    try:
        text = store.spec_path(home, s.key).read_text(encoding="utf-8")
    except (OSError, store.MaestroError):
        return ""
    return parse_title(text, s.key) or ""


def ac_hash(ac_text: str) -> str:
    """Content hash identifying one AC — invalidated by any edit to its line, so a
    human spec edit desyncs a stale attestation instead of mismatching by index."""
    return content_hash(ac_text.strip())


@dataclass
class Snapshot:
    key: str
    phase: str = Phase.TRIAGING.value
    observed_seq: int = 0
    spec_hash: str | None = None
    title: str | None = None
    source: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    pr_draft: bool | None = None
    ci_state: str | None = None
    failing_checks: list[str] = field(default_factory=list)
    unresolved_reviews: int = 0
    failure_count: int = 0
    last_error: str | None = None
    # RB-11: True when the CURRENT DEGRADED park was `burn.should_park` parking a
    # burning key (Stalled payload carries kind="burn"), not a generic dead-letter --
    # lets human-facing surfaces (status/NEEDS-YOU.md) distinguish "burning" from
    # "parked, waiting for you". Reset on every PhaseChanged, same as failure_count.
    burning: bool = False
    next_requeue_at: float | None = None
    open_questions: dict[str, str] = field(default_factory=dict)
    # qid → answer text for questions answered since the last phase change.
    # Survives crash-and-respawn so the reconciler can act on a folded answer
    # even when observed_seq has already advanced past the QuestionAnswered event.
    answered_questions: dict[str, str] = field(default_factory=dict)
    impl_turns: int = 0
    last_step: str | None = None
    kind: str = "implementation"
    proposal_path: str | None = None
    updated_ts: str | None = None
    # ac_hash -> structured evidence dict ({what, where, result}), from
    # AcVerified events. Never reset by a phase change; only a spec edit that
    # changes an AC's text invalidates an entry (its hash simply stops matching
    # any current AC — see acs_unverified()).
    ac_verified: dict[str, dict] = field(default_factory=dict)
    # AD-7: historical-only -- set once by an Approved event, from back when
    # `maestro approve` cleared a tier-2 implementing gate. Nothing emits
    # Approved anymore and nothing reads this field for gating, but it's kept
    # (never reset by a phase change, exactly as before) so a snapshot rebuilt
    # from an old log with a real Approved event still round-trips faithfully.
    approved: bool = False
    # ac_hash -> {"verdict": "pass"|"fail", "evidence": str}, from AcQaVerdict
    # events with axis "spec" (or no axis, for pre-T-23 events) — an independent
    # QA agent's re-check of "does the diff satisfy this AC?", distinct from
    # ac_verified's self-attestation. Latest verdict per hash wins (a re-check
    # after a fix overwrites the earlier fail), same content-hash-keyed
    # invalidation as ac_verified. This is the ONLY axis that gates
    # `implementing -> awaiting-ci` (see qa_failing_acs / ops._refuse_if_qa_failing) —
    # its meaning is unchanged by the standards axis below (T-23).
    qa_verdicts: dict[str, dict] = field(default_factory=dict)
    # ac_hash -> {"verdict": "pass"|"fail", "evidence": str}, from AcQaVerdict
    # events with axis "standards" (T-23) — a second, independent QA agent's
    # re-check of CLAUDE.md conventions + a Fowler-smell baseline, kept in a
    # separate bucket so it is never reranked against `qa_verdicts` (the spec
    # axis). Advisory only: a "standards" fail is visible (standards_failing_acs)
    # but, unlike a "spec" fail, does NOT block `set-phase awaiting-ci` -- an
    # explicit choice (see T-23 spec + ops._refuse_if_qa_failing).
    qa_verdicts_standards: dict[str, dict] = field(default_factory=dict)
    # Set when a ticket originated from an external tracker (e.g. Jira) so the
    # dispatcher's sync tick knows which tickets to `refresh`.
    external_source: str | None = None
    external_id: str | None = None
    # [repos.<name>] this ticket is bound to, from TicketCreated.repo. None = no
    # explicit binding -- repos.resolve() falls back to the implicit default.
    repo: str | None = None
    # tree_key -> {"command", "exit_code", "passed"}, from TestRunCaptured events
    # (RB-12) -- maestro's own captured proof, never an agent's self-attestation.
    # tree_key is "<HEAD sha>:<hash of the dirty tree>", so a record only ever
    # matches the exact tree state it was captured at; latest capture per
    # tree_key wins. Never reset by a phase change -- a passing capture stays
    # valid across a fix-round bounce as long as the tree itself hasn't moved.
    test_runs: dict[str, dict] = field(default_factory=dict)
    # tree_key -> ac_hash -> {"kind", "command", "exit_code", "passed",
    # "failure_excerpt"?}, from AcCheckCaptured events (T-79) -- the per-AC
    # counterpart to `test_runs` above, one entry per ANNOTATED AC the
    # verifying stage has checked at that tree state. Same binding rule as
    # `test_runs`: a record only ever matches the exact tree state it was
    # captured at, and is never reset by a phase change.
    ac_checks: dict[str, dict[str, dict]] = field(default_factory=dict)
    # Human-readable notes of malformed events `fold` coerced instead of
    # raising on (RB-2, law (b)) -- "seq <n> <Type>: <what was wrong>". Never
    # reset by a phase change; a corrupt log stays visible for as long as the
    # corrupt event remains in the log (a compaction doesn't remove it).
    fold_warnings: list[str] = field(default_factory=list)

    @property
    def question_open(self) -> bool:
        return bool(self.open_questions)

    def acs_unverified(self, spec_text: str, tree_key: str | None = None) -> int:
        """Count ACs in *spec_text* not yet satisfied for the `awaiting-ci` gate.

        Matching is by content hash of the AC's own line, so editing an AC's text
        (even without adding/removing checkboxes) makes its old attestation stop
        counting — the human's edit desyncs it rather than silently keeping a
        now-stale "verified" against different wording. Same rule for an
        annotation: editing or removing it invalidates any prior captured check
        for that hash exactly like a text edit does (T-79 AC2).

        `tree_key` is the T-79 annotation-aware gate switch: ``None`` (the
        default) means the annotation regime is INACTIVE for this call -- every
        AC, annotated or not, is judged purely by `ac_verified` self-attestation,
        byte-identical to before this ticket (ships dark; see
        `ops._annotations_active`). When a real `tree_key` is passed, an
        ANNOTATED AC is instead judged by whether a current-tree PASSING
        AcCheckCaptured record exists for its hash -- `verify_ac` stays
        available as narrative evidence but stops being load-bearing for that
        AC. Unannotated ACs are unaffected either way.
        """
        count = 0
        for t in parse_acs(spec_text):
            h = ac_hash(t)
            ann = parse_ac_annotation(t) if tree_key is not None else None
            if ann is not None:
                if not self.ac_check_passing(tree_key, h):
                    count += 1
            elif h not in self.ac_verified:
                count += 1
        return count

    def ac_check_record(self, tree_key: str, h: str) -> dict | None:
        return self.ac_checks.get(tree_key, {}).get(h)

    def ac_check_passing(self, tree_key: str, h: str) -> bool:
        """True iff an AcCheckCaptured record exists for AC hash *h* at the
        exact current *tree_key* and it passed -- the annotated-AC analogue of
        `tests_passing` (T-79)."""
        rec = self.ac_check_record(tree_key, h)
        return bool(rec and rec.get("passed"))

    def qa_failing_acs(self, spec_text: str) -> list[str]:
        """AC texts (in spec order) whose latest independent spec-axis QA verdict
        is "fail" — a current AC (matched by content hash) recorded as failing
        with no later passing re-check overwriting it. This is the axis that
        gates `implementing -> awaiting-ci`; the standards axis (T-23) does not
        (see standards_failing_acs)."""
        out = []
        for t in parse_acs(spec_text):
            v = self.qa_verdicts.get(ac_hash(t))
            if v and v.get("verdict") == "fail":
                out.append(t)
        return out

    def tests_passing(self, tree_key: str) -> bool:
        """True iff a TestRunCaptured record exists for *tree_key* (the exact
        current tree state) and it passed -- the whole gate, in one predicate
        (RB-12). A record from a different tree state (one more edit since it
        was captured) simply isn't in this dict at all, so it can never
        satisfy a later, different tree_key -- see the T-71 spec's "bind the
        record to the tree" note."""
        rec = self.test_runs.get(tree_key)
        return bool(rec and rec.get("passed"))

    def standards_failing_acs(self, spec_text: str) -> list[str]:
        """AC texts (in spec order) whose latest independent standards-axis QA
        verdict is "fail" (T-23, config-gated by `qa_standards_axis`). Advisory
        only — deliberately NOT consulted by `set_phase`'s awaiting-ci gate, so
        it never blocks a ticket the way qa_failing_acs does."""
        out = []
        for t in parse_acs(spec_text):
            v = self.qa_verdicts_standards.get(ac_hash(t))
            if v and v.get("verdict") == "fail":
                out.append(t)
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["question_open"] = self.question_open
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def _coerce_phase(p: dict, default: str) -> tuple[str, str | None]:
    """Best-effort ``Phase(payload['phase']).value``, falling back to
    *default* (the snapshot's current phase, i.e. treat the event as a
    phase-preserving no-op) on a missing or unrecognized value -- never
    raises (law (b): fold is total). Returns ``(phase, warning_or_None)``."""
    raw = p.get("phase")
    try:
        return Phase(raw).value, None
    except ValueError:
        return default, f"unknown/missing phase {raw!r}"


def _coerce_turn(p: dict, default: int) -> tuple[int, str | None]:
    """Best-effort ``int(payload['turn'])``, falling back to *default* (the
    snapshot's current counter, unchanged) on a non-numeric value -- never
    raises (law (b): fold is total). Returns ``(turn, warning_or_None)``."""
    raw = p.get("turn", default)
    try:
        return int(raw), None
    except (TypeError, ValueError, OverflowError):
        # OverflowError: RB-10's property generator found `int(float("inf"))` -- a value
        # `int()` accepts as an argument but can't represent -- raises OverflowError, not
        # ValueError, so it slipped past the original except clause.
        return default, f"non-integer turn {raw!r}"


def fold(key: str, events: list[dict]) -> Snapshot:
    """Replay events into a Snapshot. Pure function of the log — the whole point.

    Total (never raises — see module docstring) and DONE-absorbing. Also
    duplicate-idempotent in its own right (``fold(evs) == fold(evs * 2)``):
    *events* is deduplicated by seq (first occurrence wins) before the replay
    loop runs, so a caller handing this a raw, possibly-duplicated list —
    including ``fold`` itself, called directly, as opposed to through
    ``event_log.read``, which already dedups upstream for the same reason
    (see its docstring) — still gets a duplicate-safe fold.
    """
    seen_seqs: set[int] = set()
    deduped: list[dict] = []
    for ev in events:
        seq = ev.get("seq")
        if isinstance(seq, int):
            if seq in seen_seqs:
                continue
            seen_seqs.add(seq)
        deduped.append(ev)
    events = deduped

    s = Snapshot(key=key)
    for ev in events:
        seq = ev.get("seq")
        if isinstance(seq, int) and seq > s.observed_seq:
            s.observed_seq = seq
        t = ev.get("type")
        p = ev.get("payload")
        if not isinstance(p, dict):
            # (b) totality: a payload that isn't a dict at all (a bare int/str/list, found by
            # RB-10's property generator) must coerce to empty, not crash every `p.get(...)`
            # call below -- the same defensive posture as `_coerce_phase`/`_coerce_turn`.
            p = {}
        s.updated_ts = ev.get("ts", s.updated_ts)

        if t == E.TICKET_CREATED:
            s.title = p.get("title", s.title)
            s.source = p.get("source", s.source)
            s.spec_hash = p.get("spec_hash", s.spec_hash)
            s.kind = p.get("kind", "implementation")
            s.external_source = p.get("external_source", s.external_source)
            s.external_id = p.get("external_id", s.external_id)
            s.repo = p.get("repo", s.repo)
            if s.phase == Phase.DONE.value:
                # (e) DONE is absorbing: a (re-)TicketCreated after Finalized
                # must not resurrect a finished ticket back to TRIAGING -- the
                # write boundary already refuses to append a second
                # TicketCreated to a key with events, but `fold` itself must
                # hold this law for ANY event list, not just ones the write
                # boundary would actually produce.
                s.fold_warnings.append(f"seq {seq} {t}: dropped phase reset -- phase is DONE (absorbing)")
            else:
                s.phase = Phase.TRIAGING.value
        elif t == E.SPEC_OBSERVED:
            s.spec_hash = p.get("spec_hash", s.spec_hash)
        elif t == E.PHASE_CHANGED:
            if s.phase == Phase.DONE.value:
                # (e) DONE is absorbing: a PhaseChanged after Finalized is a
                # full no-op, not just a phase no-op -- record it and move on.
                s.fold_warnings.append(f"seq {seq} {t}: dropped -- phase is DONE (absorbing)")
            else:
                new_phase, warn = _coerce_phase(p, s.phase)
                s.phase = new_phase
                s.failure_count = 0
                s.burning = False
                s.next_requeue_at = None
                s.answered_questions = {}
                s.unresolved_reviews = 0
                if warn:
                    s.fold_warnings.append(f"seq {seq} {t}: {warn}")
        elif t == E.QUESTION_ASKED:
            s.open_questions[p.get("qid", str(seq))] = p.get("text", "")
        elif t == E.QUESTION_ANSWERED:
            qid = p.get("qid")
            s.open_questions.pop(qid, None)
            if qid:
                s.answered_questions[qid] = p.get("answer", "")
        elif t == E.PR_OPENED:
            s.pr_number = p.get("number", s.pr_number)
            s.pr_url = p.get("url", s.pr_url)
            s.pr_draft = p.get("draft", True)
            s.pr_state = "open"
        elif t == E.PR_UPDATED:
            if p.get("merged"):
                s.pr_state = "merged"
            if "draft" in p:
                s.pr_draft = p["draft"]
        elif t == E.CI_OBSERVED:
            s.ci_state = p.get("state", s.ci_state)
            s.failing_checks = p.get("failing_checks", [])
        elif t == E.REVIEW_FEEDBACK_RECEIVED:
            if p.get("state") == "CHANGES_REQUESTED":
                s.unresolved_reviews += 1
        elif t == E.IMPL_TURN:
            turn, warn = _coerce_turn(p, s.impl_turns)
            s.impl_turns = max(s.impl_turns, turn)
            if warn:
                s.fold_warnings.append(f"seq {seq} {t}: {warn}")
        elif t == E.IMPL_STEP:
            turn, warn = _coerce_turn(p, s.impl_turns)
            s.impl_turns = max(s.impl_turns, turn)
            if warn:
                s.fold_warnings.append(f"seq {seq} {t}: {warn}")
            if p.get("summary"):
                s.last_step = p["summary"]
        elif t == E.AC_VERIFIED:
            h = p.get("ac_hash")
            if h:
                s.ac_verified[h] = p.get("evidence", {})
        elif t == E.AC_QA_VERDICT:
            h = p.get("ac_hash")
            if h:
                entry = {"verdict": p.get("verdict"), "evidence": p.get("evidence", "")}
                if p.get("axis") == "standards":
                    s.qa_verdicts_standards[h] = entry
                else:
                    s.qa_verdicts[h] = entry  # axis "spec", or absent (pre-T-23 events)
        elif t == E.TEST_RUN_CAPTURED:
            tk = p.get("tree_key")
            if tk:
                rec = {
                    "command": p.get("command"),
                    "exit_code": p.get("exit_code"),
                    "passed": bool(p.get("passed")),
                }
                # RB-14: only present on a failing record (see events.py) --
                # carried into the snapshot so a CACHED record (one already
                # captured before `verifying` re-checks it) still has an
                # excerpt to route with, not just a freshly-folded one.
                if p.get("failure_excerpt"):
                    rec["failure_excerpt"] = p["failure_excerpt"]
                s.test_runs[tk] = rec
        elif t == E.AC_CHECK_CAPTURED:
            tk = p.get("tree_key")
            h = p.get("ac_hash")
            if tk and h:
                rec = {
                    "kind": p.get("kind"),
                    "command": p.get("command"),
                    "exit_code": p.get("exit_code"),
                    "passed": bool(p.get("passed")),
                }
                if p.get("failure_excerpt"):
                    rec["failure_excerpt"] = p["failure_excerpt"]
                s.ac_checks.setdefault(tk, {})[h] = rec
        elif t == E.RESEARCH_PROPOSED:
            s.proposal_path = p.get("proposal_path", s.proposal_path)
        elif t == E.APPROVED:
            s.approved = True  # AD-7: historical-only, see events.APPROVED/Snapshot.approved
        elif t == E.REQUEUE_SCHEDULED:
            s.next_requeue_at = p.get("at")
        elif t == E.FAILED:
            s.failure_count += 1
            s.last_error = p.get("error", s.last_error)
        elif t == E.STALLED:
            if s.phase == Phase.DONE.value:
                # (e) DONE is absorbing: a Stalled after Finalized cannot
                # resurrect a finished ticket into an active phase.
                s.fold_warnings.append(f"seq {seq} {t}: dropped -- phase is DONE (absorbing)")
            else:
                s.phase = Phase.DEGRADED.value
                s.last_error = p.get("reason", s.last_error)
                s.burning = p.get("kind") == "burn"
        elif t == E.FINALIZED:
            s.phase = Phase.DONE.value
            s.next_requeue_at = None
    return s


def rebuild(home: Path, key: str) -> Snapshot:
    """Fold the full log and atomically persist the snapshot."""
    snap = fold(key, event_log.read(home, key))
    store.write_json(store.snapshot_path(home, key), snap.to_dict())
    return snap


def load(home: Path, key: str) -> Snapshot:
    """Load the persisted snapshot, or a fresh TRIAGING default if none exists.

    Falls back to the archived location (``archive_done`` relocates a DONE
    ticket's snapshot there): otherwise a dependent whose ``dependsOn`` entry
    finished and got archived would see a phantom fresh-TRIAGING snapshot and
    block forever on a dependency that actually completed.
    """
    d = store.read_json(store.snapshot_path(home, key))
    if not d:
        d = store.read_json(store.archived_snapshot_path(home, key))
    if not d:
        return Snapshot(key=key)
    return Snapshot.from_dict(d)
