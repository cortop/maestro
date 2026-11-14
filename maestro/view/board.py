"""Board view-model -- ticket rows, the named filters and their counts."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import claims, fleet as fleet_mod, projection, snapshot as snap_mod, steplog, store
from ..dispatcher import split_key
from ..config import Config
from ..statemachine import ACTIVE_PHASES, Phase

NEEDS_YOU_PHASES = frozenset({Phase.AWAITING_HUMAN, Phase.DEGRADED})
NEEDS_YOU_PHASE_VALUES = {p.value for p in NEEDS_YOU_PHASES}
_REVIEW_PHASES = frozenset({Phase.AWAITING_CI, Phase.IN_REVIEW})

Predicate = Callable[[Path, snap_mod.Snapshot], bool]


def needs_you(home: Path, s: snap_mod.Snapshot) -> bool:
    """The two sleeping-and-stuck phases."""
    del home
    return s.phase in NEEDS_YOU_PHASE_VALUES


def held(home: Path, s: snap_mod.Snapshot) -> bool:
    return fleet_mod.hold_state(home, s.key, store.now_epoch()) is not None


# Named filters in display order; `running` is resolved against live claims (`board`), and `all`
# has no predicate.
FILTERS: list[tuple[str, Predicate | None]] = [
    ("needs-you", needs_you),
    ("active", projection.phase_predicate(ACTIVE_PHASES)),
    ("review", projection.phase_predicate(_REVIEW_PHASES)),
    ("held", held),
    ("running", None),
    ("all", None),
]
FILTER_NAMES = tuple(name for name, _ in FILTERS)


# Sortable columns in table order; the numeric ones sort descending on their first click.
SORT_COLUMNS = ("key", "phase", "title", "pr", "ci", "fails")
NUMERIC_SORT = frozenset({"pr", "fails"})


@dataclass(frozen=True)
class BoardRow:
    key: str
    phase: str
    title: str
    pr: str
    ci: str
    fails: str
    held: bool = False
    running: bool = False
    now: str = ""
    pr_number: int = 0
    failure_count: int = 0


@dataclass
class BoardView:
    filter: str
    rows: list[BoardRow]
    counts: dict[str, int] = field(default_factory=dict)
    search: str = ""
    sort: str = ""
    desc: bool = False

    @property
    def needs_you(self) -> int:
        return self.counts.get("needs-you", 0)


def running_keys(home: Path, cfg: Config) -> frozenset[str]:
    """Keys with a live claim (read-only: `describe_claims` never releases one)."""
    return frozenset(r["key"] for r in claims.describe_claims(
        home, max_age=cfg.unverified_claim_max_age) if r["claimed"])


_LIVE_STATES: dict[str, dict] = {}


def live_statuses(home: Path, running: frozenset[str]) -> dict[str, dict]:
    """`steplog.live_status` for each running key's claimed session log (incremental across
    calls). Blocking on the first read of a large log: call off the UI thread."""
    out: dict[str, dict] = {}
    for key in running:
        claim = claims.read_claim(home, key) or {}
        log = claim.get("log_path")
        if not log:
            continue
        status, _LIVE_STATES[log] = steplog.live_status(Path(log), _LIVE_STATES.get(log))
        out[key] = status
    return out


def live_lines(status: dict | None, now: float) -> tuple[str, str]:
    """``(doing, now)`` texts for a live status; blank for a key with none."""
    return steplog.live_texts(status, now) if status else ("", "")


def select(home: Path, snaps: list[snap_mod.Snapshot], name: str,
           running: frozenset[str] = frozenset()) -> list[snap_mod.Snapshot]:
    """The snapshots the named filter shows; an unknown name raises ValueError."""
    if name not in FILTER_NAMES:
        raise ValueError(f"unknown filter {name!r}; expected one of {', '.join(FILTER_NAMES)}")
    if name == "all":
        return list(snaps)
    if name == "running":
        return [s for s in snaps if s.key in running]
    pred = dict(FILTERS)[name]
    return [s for s in snaps if pred(home, s)]


def matches_search(row: BoardRow, needle: str) -> bool:
    """Case-insensitive substring match on the row's key or title; a blank needle matches all."""
    needle = needle.strip().lower()
    return not needle or needle in row.key.lower() or needle in row.title.lower()


def sort_rows(rows: list[BoardRow], column: str, desc: bool) -> list[BoardRow]:
    """Stable sort of *rows* by one of `SORT_COLUMNS`; an unknown column raises ValueError."""
    if column not in SORT_COLUMNS:
        raise ValueError(f"unknown sort column {column!r}; expected one of {', '.join(SORT_COLUMNS)}")
    keyfn = {
        "key": lambda r: split_key(r.key),
        "phase": lambda r: projection._PHASE_RANK.get(r.phase, 99),
        "title": lambda r: r.title.lower(),
        "pr": lambda r: r.pr_number,
        "ci": lambda r: r.ci.lower(),
        "fails": lambda r: r.failure_count,
    }[column]
    return sorted(rows, key=keyfn, reverse=desc)


def board(home: Path, cfg: Config, name: str = "needs-you", search: str = "",
          sort: str = "", desc: bool = False) -> BoardView:
    """Rows for filter *name* (table order, or by *sort*) matching *search*, plus every filter's count."""
    snaps = projection.load_snapshots(home)
    running = running_keys(home, cfg)
    counts = {n: len(select(home, snaps, n, running)) for n in FILTER_NAMES}
    shown = {s.key for s in select(home, snaps, name, running)}
    live = live_statuses(home, running)
    now = store.now_epoch()
    rows = []
    for cells in projection.rows_from_snapshots(home, [s for s in snaps if s.key in shown]):
        key = cells[-1]
        snap = snap_mod.load(home, key)
        row = BoardRow(key=key, phase=cells[1], title=cells[2], pr=cells[3], ci=cells[4],
                       fails=cells[5], held=held(home, snap), running=key in running,
                       now=live_lines(live.get(key), now)[0],
                       pr_number=snap.pr_number or 0, failure_count=snap.failure_count)
        if matches_search(row, search):
            rows.append(row)
    if sort:
        rows = sort_rows(rows, sort, desc)
    return BoardView(filter=name, rows=rows, counts=counts, search=search.strip(), sort=sort,
                     desc=desc)
