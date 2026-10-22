"""Board view-model -- ticket rows, the named filters and their counts."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import claims, fleet as fleet_mod, projection, snapshot as snap_mod, store
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


@dataclass
class BoardView:
    filter: str
    rows: list[BoardRow]
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def needs_you(self) -> int:
        return self.counts.get("needs-you", 0)


def running_keys(home: Path, cfg: Config) -> frozenset[str]:
    """Keys with a live claim (read-only: `describe_claims` never releases one)."""
    return frozenset(r["key"] for r in claims.describe_claims(
        home, max_age=cfg.unverified_claim_max_age) if r["claimed"])


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


def board(home: Path, cfg: Config, name: str = "needs-you") -> BoardView:
    """Rows for filter *name* (table order) plus every filter's count."""
    snaps = projection.load_snapshots(home)
    running = running_keys(home, cfg)
    counts = {n: len(select(home, snaps, n, running)) for n in FILTER_NAMES}
    shown = {s.key for s in select(home, snaps, name, running)}
    rows = []
    for cells in projection.rows_from_snapshots(home, [s for s in snaps if s.key in shown]):
        key = cells[-1]
        rows.append(BoardRow(key=key, phase=cells[1], title=cells[2], pr=cells[3], ci=cells[4],
                             fails=cells[5], held=held(home, snap_mod.load(home, key)),
                             running=key in running))
    return BoardView(filter=name, rows=rows, counts=counts)
