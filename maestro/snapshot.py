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
from typing import Callable

from . import events as E
from . import event_log, store
from .idempotency import content_hash
from .statemachine import Phase

# Spec acceptance criteria are Markdown task-list items: "- [ ] ..." / "- [x] ...".
# T-112: the gap between "]" and the captured text is `[ \t]*`, NOT `\s*` --
# `\s` matches a newline too, so a blank "- [ ] " placeholder line
# immediately followed by another AC line would otherwise have its trailing
# whitespace run bleed across the line break and swallow the next line's
# checkbox+text into this one's capture (a match spanning two lines instead
# of one) -- exactly the adjacency `ops.add_ac` (T-112) produces when a
# human appends their first real AC right after the seed template's own
# dangling placeholder.
_AC_RE = re.compile(r"^- \[[ xX]\][ \t]*(.+)$", re.MULTILINE)

# A spec's title is its first level-1 heading, conventionally "# <KEY>: <title>".
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# T-79: an opt-in, machine-checkable annotation trailing an AC line --
# `(test: <path>)`, `(test: <path>::<id>)`, or `(check: <shell command>)`.
# Finds the LAST `(test:`/`(check:` in the line -- `parse_ac_annotation`
# below then hand-scans forward for the PAREN-BALANCED close, so a `check:`
# body may itself contain parens (T-98: e.g. a Bazel `--test_filter=` regex,
# `--test_filter='^(A|B)$'`) without truncating early. Anchored at
# end-of-line (nothing but whitespace may follow the balanced close) is what
# keeps a SECOND trailing parenthetical, e.g. "(test: a.py) (see #123)",
# from ever being swallowed into the annotation -- any other trailing
# parenthetical (e.g. "(checked manually)") simply doesn't match this start
# pattern and the AC stays plain text, exactly as before T-79.
_AC_ANNOTATION_START_RE = re.compile(r"\((test|check):\s*")


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
    the feature ship dark by construction.

    T-98: the body is found by a paren-BALANCED scan (never a `[^()]` ban),
    so a `check:` command containing its own parens (a Bazel `--test_filter`
    regex, say) parses correctly instead of silently degrading to the prose
    tier -- the balanced close must still be the very last non-whitespace
    character on the line, which is what keeps a genuine second trailing
    parenthetical from being swallowed (see `_AC_ANNOTATION_START_RE`)."""
    start = None
    for m in _AC_ANNOTATION_START_RE.finditer(ac_text):
        start = m  # the LAST candidate start -- closest to end-of-line wins
    if start is None:
        return None
    kind = start.group(1)
    body_start = start.end()
    depth = 1  # the opening '(' the start pattern itself consumed
    close_idx = None
    for i in range(body_start, len(ac_text)):
        c = ac_text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                close_idx = i
                break
    if close_idx is None or ac_text[close_idx + 1:].strip():
        return None  # unbalanced, or non-whitespace trails the close
    body = ac_text[body_start:close_idx].strip()
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


def has_acs(spec_text: str) -> bool:
    """True iff *spec_text* parses to at least one NON-BLANK acceptance
    criterion -- the one canonical "zero ACs" definition every T-80 gate
    (the dispatcher due-gate, `ops.set_phase`'s fail-closed handoff, `ops.qa_brief`,
    `health.check_missing_acs`, `maestro create`'s mint-time warning) shares.

    A bare ``- `` bullet (no checkbox) matches ``_AC_RE`` not at all, so
    `parse_acs` returns no entries for it -- already zero. But the seed
    template's own dangling ``- [ ] `` (checkbox, no text) matches once, with
    an empty-string capture -- `len(parse_acs(...)) == 0` alone would miss
    this, the single most common freshly-created-ticket shape. Checking for
    any NON-BLANK entry (`parse_acs` already `.strip()`s each one) catches
    both shapes uniformly."""
    return any(parse_acs(spec_text))


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
    # T-89 (AC5): the `kind`/`state` markers off the CURRENT DEGRADED park's Failed/
    # Stalled payload -- e.g. kind="provider", state="no_network" when the fleet's
    # provider_availability check was non-ok at fail time, so the TUI detail pane can
    # read a degraded ticket as provider-caused rather than a bare watchdog timeout.
    # None for a generic failure. Reset on every PhaseChanged, same as burning.
    last_error_kind: str | None = None
    last_error_state: str | None = None
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
    # T-123: head_sha -> {"at": epoch, "run_ids": [...]}, from CiRerunRequested
    # events -- one entry per head SHA, ever (idempotent by step_id), so
    # `dispatcher._observe_ci` can ask "was this SHA already re-run, and how
    # long ago?" to gate the ci_rerun_grace window and to fold a marker into
    # the CiObserved step-id so a post-rerun poll still produces a fresh event
    # even when its content is byte-identical to the pre-rerun one. Never
    # reset by a phase change -- a head SHA's one rerun stays recorded across
    # a fix-round bounce as long as the SHA itself hasn't moved.
    ci_reruns: dict[str, dict] = field(default_factory=dict)
    # T-126: an approved PR split's ordered stack, from PrOpened events whose
    # payload carries a "stack" sub-dict ({index, total, number, url, branch,
    # base}) -- a plain (non-split) PrOpened carries no such field and this
    # stays empty, so a threshold-0/under-threshold ticket is byte-identical
    # to before this field existed. Each entry: {index, total, number, url,
    # branch, base, merged, ci_state, qa_verdict}. `pr_number`/`pr_url`/
    # `pr_state`/`pr_draft` above always mirror whichever PR is CURRENTLY
    # being polled -- entry 0 at open time, advancing to the next unmerged
    # entry as `ops.check_merged` folds a further PrOpened each time one
    # merges (see that function's docstring). `ci_state`/`qa_verdict` treat
    # each entry as its own subticket: a CiObserved or a qa-gated
    # PhaseChanged into AWAITING_CI is stamped onto whichever entry is
    # CURRENTLY the active poll target at that point in the fold, so an
    # entry's status survives the mirror later advancing past it -- a human
    # reading `pr_stack` can see entry 0 passed CI/QA even after entry 1
    # becomes the active poll target.
    pr_stack: list[dict] = field(default_factory=list)
    # T-128: comment_id -> [tree_sha, ...] already replied to, from
    # ReviewReplyPosted events -- `ops.reply_review` consults this BEFORE
    # calling the VCS provider, so a re-run at the same tree state (the worker
    # cwd's HEAD sha unchanged) never posts a duplicate reply. Never reset by a
    # phase change -- a comment's reply history survives a fix-round bounce.
    review_replies: dict[str, list[str]] = field(default_factory=dict)

    @property
    def question_open(self) -> bool:
        return bool(self.open_questions)

    @property
    def display_key(self) -> str:
        """The tracker's own identifier (e.g. `BDA-123`) if this ticket was
        imported from one, else the maestro key itself (T-134) -- what PR
        titles should use instead of `self.key`, so a Linear-imported
        `LINEAR-BDA-123` ticket's PRs read `BDA-123: ...` the way reviewers
        and Linear's own PR linking expect."""
        return self.external_id or self.key

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

    def qa_all_passing(self, spec_text: str) -> bool:
        """True iff EVERY current AC (by content hash) carries a spec-axis QA
        verdict of "pass" -- the positive counterpart to `qa_failing_acs`: an AC
        with no verdict at all (never independently QA'd) does NOT count as
        passing, only an explicit "pass" does. Used to gate T-86's undraft step.
        Equivalent to `not qa_unpassed_acs(spec_text)`, kept as its own boolean
        predicate since T-86 only ever needs the yes/no answer."""
        return not self.qa_unpassed_acs(spec_text)

    def qa_unpassed_acs(self, spec_text: str) -> list[str]:
        """AC texts (in spec order) that do NOT carry a latest spec-axis QA verdict of
        "pass" -- i.e. no verdict recorded at all, or the latest one is "fail" (T-85).
        Stricter than `qa_failing_acs`: that one only catches a recorded fail, so zero
        verdicts on an AC used to satisfy the `awaiting-ci` gate silently; this is what
        `ops._refuse_if_qa_incomplete` requires be empty (config-gated by
        `awaiting_ci_qa_gate`) before `implementing -> awaiting-ci` is allowed."""
        out = []
        for t in parse_acs(spec_text):
            v = self.qa_verdicts.get(ac_hash(t))
            if not v or v.get("verdict") != "pass":
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
        d["display_key"] = self.display_key
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


# --- fold: one small handler per event type ----------------------------------
#
# Each handler takes (snapshot, payload, seq, event_type) and mutates the
# snapshot. Handlers must be total (never raise -- law (b)): read the payload
# with `.get`, coerce through `_coerce_*`, and record a `fold_warnings` entry
# instead of failing. Event types with no handler fold to nothing beyond
# `observed_seq`/`updated_ts` and must be listed in `_UNFOLDED`, so a new
# event type can't be silently ignored (tests/test_snapshot.py checks this).


def _warn(s: Snapshot, seq, t: str, msg: str) -> None:
    s.fold_warnings.append(f"seq {seq} {t}: {msg}")


def _stack_entry(s: Snapshot, *, number=None, index=None) -> dict | None:
    """The `pr_stack` row with this PR number (or stack index), if any."""
    for e in s.pr_stack:
        if (index is not None and e.get("index") == index) or \
                (index is None and e.get("number") == number):
            return e
    return None


def _fold_ticket_created(s: Snapshot, p: dict, seq, t: str) -> None:
    s.title = p.get("title", s.title)
    s.source = p.get("source", s.source)
    s.spec_hash = p.get("spec_hash", s.spec_hash)
    s.kind = p.get("kind", "implementation")
    s.external_source = p.get("external_source", s.external_source)
    s.external_id = p.get("external_id", s.external_id)
    s.repo = p.get("repo", s.repo)
    s.phase = Phase.TRIAGING.value


def _fold_spec_observed(s: Snapshot, p: dict, seq, t: str) -> None:
    s.spec_hash = p.get("spec_hash", s.spec_hash)


def _fold_phase_changed(s: Snapshot, p: dict, seq, t: str) -> None:
    new_phase, warn = _coerce_phase(p, s.phase)
    # T-126: a qa-gated hop into AWAITING_CI (the qa skill's set-phase reason
    # always starts "qa:") means every current AC just passed spec-axis QA --
    # stamp that on the stack entry currently being polled, so each stacked
    # PR keeps its own QA record after the mirror advances past it.
    if (new_phase == Phase.AWAITING_CI.value
            and p.get("reason", "").startswith("qa:") and s.pr_stack):
        entry = _stack_entry(s, number=s.pr_number)
        if entry is not None:
            entry["qa_verdict"] = "pass"
    s.phase = new_phase
    s.failure_count = 0
    s.burning = False
    s.last_error_kind = None
    s.last_error_state = None
    s.next_requeue_at = None
    s.answered_questions = {}
    s.unresolved_reviews = 0
    if warn:
        _warn(s, seq, t, warn)


def _fold_question_asked(s: Snapshot, p: dict, seq, t: str) -> None:
    s.open_questions[p.get("qid", str(seq))] = p.get("text", "")


def _fold_question_answered(s: Snapshot, p: dict, seq, t: str) -> None:
    qid = p.get("qid")
    s.open_questions.pop(qid, None)
    if qid:
        s.answered_questions[qid] = p.get("answer", "")


def _fold_pr_opened(s: Snapshot, p: dict, seq, t: str) -> None:
    # T-126: a split-approved open carries `stack` metadata and records its
    # own `pr_stack` row. Only a plain open (or stack index 0) moves the
    # ticket-wide pr_* mirror, which always tracks the PR currently being
    # polled; `ops.check_merged` advances it with a further plain PrOpened.
    stack_meta = p.get("stack")
    is_stack_entry = isinstance(stack_meta, dict) and isinstance(stack_meta.get("index"), int)
    if is_stack_entry:
        entry = {
            "index": stack_meta["index"], "total": stack_meta.get("total"),
            "number": p.get("number"), "url": p.get("url"),
            "branch": stack_meta.get("branch"), "base": stack_meta.get("base"),
            "merged": False, "ci_state": None, "qa_verdict": None,
            # T-131: opened --draft, like the ticket-wide mirror below.
            "draft": p.get("draft", True),
        }
        s.pr_stack = [e for e in s.pr_stack if e.get("index") != entry["index"]] + [entry]
    if not is_stack_entry or stack_meta.get("index") == 0:
        s.pr_number = p.get("number", s.pr_number)
        s.pr_url = p.get("url", s.pr_url)
        s.pr_draft = p.get("draft", True)
        s.pr_state = "open"


def _fold_pr_updated(s: Snapshot, p: dict, seq, t: str) -> None:
    stack_idx = p.get("stack_index")
    if isinstance(stack_idx, int):
        # T-126/T-131: a stack-bookkeeping update touches only its own row,
        # never the ticket-wide mirror.
        entry = _stack_entry(s, index=stack_idx)
        if entry is not None:
            if "merged" in p:
                entry["merged"] = bool(p.get("merged", entry.get("merged")))
            if "draft" in p:
                entry["draft"] = p["draft"]
        return
    # Scoped to the currently mirrored PR: an update about any other PR must
    # never touch the mirror (the 2026-09-24 "tip undrafted instead of root"
    # incident). A missing "number" -- every non-stack ticket's history --
    # defaults to matching.
    if p.get("number", s.pr_number) != s.pr_number:
        return
    if p.get("merged"):
        s.pr_state = "merged"
    if "draft" in p:
        s.pr_draft = p["draft"]
        # T-131: keep the tracked entry's own row in step, or the root-first
        # undraft check would see the root as perpetually draft.
        entry = _stack_entry(s, number=s.pr_number)
        if entry is not None:
            entry["draft"] = p["draft"]


def _fold_ci_observed(s: Snapshot, p: dict, seq, t: str) -> None:
    # T-130: a stacked entry's observation carries its own `pr_number`; only
    # the currently tracked PR's moves the ticket-wide ci_state mirror, but
    # every entry's own `pr_stack` row is kept current.
    ev_pr = p.get("pr_number")
    if ev_pr is None or ev_pr == s.pr_number:
        s.ci_state = p.get("state", s.ci_state)
        s.failing_checks = p.get("failing_checks", [])
    if s.pr_stack:
        entry = _stack_entry(s, number=ev_pr if ev_pr is not None else s.pr_number)
        if entry is not None:
            entry["ci_state"] = p.get("state", entry.get("ci_state"))


def _fold_ci_rerun_requested(s: Snapshot, p: dict, seq, t: str) -> None:
    head_sha = p.get("head_sha")
    if head_sha:
        s.ci_reruns[head_sha] = {"at": p.get("at"), "run_ids": p.get("run_ids", [])}


def _fold_review_feedback_received(s: Snapshot, p: dict, seq, t: str) -> None:
    # T-130: same scoping as CiObserved -- only a review of the tracked PR
    # counts toward `unresolved_reviews` (what gates undraft).
    ev_pr = p.get("pr_number")
    if p.get("state") == "CHANGES_REQUESTED" and (ev_pr is None or ev_pr == s.pr_number):
        s.unresolved_reviews += 1


def _fold_review_reply_posted(s: Snapshot, p: dict, seq, t: str) -> None:
    cid, tree_sha = p.get("comment_id"), p.get("tree_sha")
    if cid and tree_sha:
        seen = s.review_replies.setdefault(cid, [])
        if tree_sha not in seen:
            seen.append(tree_sha)


def _fold_impl_turn(s: Snapshot, p: dict, seq, t: str) -> None:
    turn, warn = _coerce_turn(p, s.impl_turns)
    s.impl_turns = max(s.impl_turns, turn)
    if warn:
        _warn(s, seq, t, warn)


def _fold_impl_step(s: Snapshot, p: dict, seq, t: str) -> None:
    _fold_impl_turn(s, p, seq, t)
    if p.get("summary"):
        s.last_step = p["summary"]


def _fold_ac_verified(s: Snapshot, p: dict, seq, t: str) -> None:
    h = p.get("ac_hash")
    if h:
        s.ac_verified[h] = p.get("evidence", {})


def _fold_ac_qa_verdict(s: Snapshot, p: dict, seq, t: str) -> None:
    h = p.get("ac_hash")
    if h:
        entry = {"verdict": p.get("verdict"), "evidence": p.get("evidence", "")}
        if p.get("axis") == "standards":
            s.qa_verdicts_standards[h] = entry
        else:
            s.qa_verdicts[h] = entry  # axis "spec", or absent (pre-T-23 events)


def _capture_record(p: dict, **extra) -> dict:
    """The shared shape of a TestRunCaptured / AcCheckCaptured record. RB-14:
    `failure_excerpt` (failing records only) is kept so a cached record can
    still be routed on, not just a freshly folded one."""
    rec = {**extra, "command": p.get("command"), "exit_code": p.get("exit_code"),
           "passed": bool(p.get("passed"))}
    if p.get("failure_excerpt"):
        rec["failure_excerpt"] = p["failure_excerpt"]
    return rec


def _fold_test_run_captured(s: Snapshot, p: dict, seq, t: str) -> None:
    tk = p.get("tree_key")
    if tk:
        s.test_runs[tk] = _capture_record(p)


def _fold_ac_check_captured(s: Snapshot, p: dict, seq, t: str) -> None:
    tk, h = p.get("tree_key"), p.get("ac_hash")
    if tk and h:
        s.ac_checks.setdefault(tk, {})[h] = _capture_record(p, kind=p.get("kind"))


def _fold_research_proposed(s: Snapshot, p: dict, seq, t: str) -> None:
    s.proposal_path = p.get("proposal_path", s.proposal_path)


def _fold_approved(s: Snapshot, p: dict, seq, t: str) -> None:
    s.approved = True  # AD-7: historical-only, see events.APPROVED/Snapshot.approved


def _fold_requeue_scheduled(s: Snapshot, p: dict, seq, t: str) -> None:
    s.next_requeue_at = p.get("at")


def _fold_failed(s: Snapshot, p: dict, seq, t: str) -> None:
    s.failure_count += 1
    s.last_error = p.get("error", s.last_error)
    s.last_error_kind = p.get("kind")
    s.last_error_state = p.get("state")


def _fold_stalled(s: Snapshot, p: dict, seq, t: str) -> None:
    s.phase = Phase.DEGRADED.value
    s.last_error = p.get("reason", s.last_error)
    s.burning = p.get("kind") == "burn"
    s.last_error_kind = p.get("kind")
    s.last_error_state = p.get("state")
    # Clear the (necessarily elapsed) backoff timer, as every phase-moving
    # event does: left in place, `is_due` answers "timer" above the sleeping
    # gate and DEGRADED never actually sleeps (T-65; 218 no-op Checked
    # events on the dogfood board, 2026-09-11).
    s.next_requeue_at = None


def _fold_finalized(s: Snapshot, p: dict, seq, t: str) -> None:
    s.phase = Phase.DONE.value
    s.next_requeue_at = None


_FOLDERS: dict[str, Callable[[Snapshot, dict, object, str], None]] = {
    E.TICKET_CREATED: _fold_ticket_created,
    E.SPEC_OBSERVED: _fold_spec_observed,
    E.PHASE_CHANGED: _fold_phase_changed,
    E.QUESTION_ASKED: _fold_question_asked,
    E.QUESTION_ANSWERED: _fold_question_answered,
    E.PR_OPENED: _fold_pr_opened,
    E.PR_UPDATED: _fold_pr_updated,
    E.CI_OBSERVED: _fold_ci_observed,
    E.CI_RERUN_REQUESTED: _fold_ci_rerun_requested,
    E.REVIEW_FEEDBACK_RECEIVED: _fold_review_feedback_received,
    E.REVIEW_REPLY_POSTED: _fold_review_reply_posted,
    E.IMPL_TURN: _fold_impl_turn,
    E.IMPL_STEP: _fold_impl_step,
    E.AC_VERIFIED: _fold_ac_verified,
    E.AC_QA_VERDICT: _fold_ac_qa_verdict,
    E.TEST_RUN_CAPTURED: _fold_test_run_captured,
    E.AC_CHECK_CAPTURED: _fold_ac_check_captured,
    E.RESEARCH_PROPOSED: _fold_research_proposed,
    E.APPROVED: _fold_approved,
    E.REQUEUE_SCHEDULED: _fold_requeue_scheduled,
    E.FAILED: _fold_failed,
    E.STALLED: _fold_stalled,
    E.FINALIZED: _fold_finalized,
}

# Event types that deliberately fold to nothing here: audit breadcrumbs, or
# facts their own readers scan the log for directly (sync cursors, the post-QA
# skill's once-per-pass marker, the answered-command inbox record).
_UNFOLDED = frozenset({
    E.NOTE, E.CHECKED, E.COMMAND_RECEIVED, E.JIRA_SYNCED, E.LINEAR_SYNCED,
    E.LINEAR_STATUS_PUSHED, E.POST_QA_SKILL_SPAWNED,
})

# (e) DONE is absorbing. These event types are dropped outright once a ticket
# is DONE -- a full no-op, not just a phase no-op -- because applying any part
# of them (resetting counters, marking DEGRADED) would describe a live ticket.
_DROPPED_WHEN_DONE = frozenset({E.PHASE_CHANGED, E.STALLED})


def fold(key: str, events: list[dict]) -> Snapshot:
    """Replay events into a Snapshot. Pure function of the log — the whole point.

    Total (never raises — see module docstring) and DONE-absorbing: once
    folded to DONE, no event moves the phase again (enforced here, for every
    handler). Also duplicate-idempotent in its own right (``fold(evs) ==
    fold(evs * 2)``): *events* is deduplicated by seq (first occurrence wins)
    before the replay loop runs, so a caller handing this a raw,
    possibly-duplicated list still gets a duplicate-safe fold.
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

    s = Snapshot(key=key)
    for ev in deduped:
        seq = ev.get("seq")
        if isinstance(seq, int) and seq > s.observed_seq:
            s.observed_seq = seq
        t = ev.get("type")
        p = ev.get("payload")
        if not isinstance(p, dict):
            # (b) totality: a non-dict payload (RB-10's property generator
            # found bare ints/strs/lists) folds as empty.
            p = {}
        s.updated_ts = ev.get("ts", s.updated_ts)

        handler = _FOLDERS.get(t)
        if handler is None:
            continue
        was_done = s.phase == Phase.DONE.value
        if was_done and t in _DROPPED_WHEN_DONE:
            _warn(s, seq, t, "dropped -- phase is DONE (absorbing)")
            continue
        handler(s, p, seq, t)
        if was_done and s.phase != Phase.DONE.value:
            # Any other handler that would move the phase (a re-TicketCreated
            # resetting to TRIAGING) keeps its other effects but not the move.
            s.phase = Phase.DONE.value
            _warn(s, seq, t, "dropped phase reset -- phase is DONE (absorbing)")
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
