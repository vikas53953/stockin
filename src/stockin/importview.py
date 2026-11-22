"""What the Import and Reconcile pages draw, laid out from objects the
ledger and the reconciler already computed.

Pure view model. Coverage turns statement windows into a month grid per
source; reconcile turns checks into counts and twin bars. Every label is
built from the same numbers as the bar beside it, and nothing here can
place an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .book import Check
from .money import Qty
from .store import Period

SOURCE_LABEL = {
    "cams_kfin_cas": "Mutual funds (CAMS / KFintech)",
    "nsdl_cas": "Demat (NSDL)",
    "cdsl_cas": "Demat (CDSL)",
}
MAX_MONTHS = 24
"""The grid shows at most the latest two years of months."""

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def source_label(source: str) -> str:
    return SOURCE_LABEL.get(source, source)


def _month(d: date) -> tuple[int, int]:
    return d.year, d.month


def _label(ym: tuple[int, int]) -> str:
    return f"{_MONTHS[ym[1] - 1]} {ym[0]}"


def _span(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = a
    while (y, m) <= b:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


@dataclass(frozen=True)
class Lane:
    label: str
    covered: tuple[bool, ...]  # one per month of Coverage.months
    gaps: tuple[str, ...]  # months with no statement between this source's first and last


@dataclass(frozen=True)
class Coverage:
    months: tuple[str, ...]  # "Jan 2026", oldest first
    lanes: tuple[Lane, ...]
    statements: int
    headline: str  # "3 statements cover Jan 2026 to Mar 2026."
    gap_lines: tuple[str, ...]  # "No Demat (CDSL) statement for Jan 2026, Feb 2026."


def coverage(periods: tuple[Period, ...]) -> Coverage | None:
    """Which months each source's statements cover. A month counts as covered
    when any statement from that source includes at least one of its days."""
    if not periods:
        return None
    first = min(_month(p.start) for p in periods)
    last = max(_month(p.end) for p in periods)
    months = _span(first, last)[-MAX_MONTHS:]
    by: dict[str, set[tuple[int, int]]] = {}
    for p in periods:
        by.setdefault(p.source, set()).update(_span(_month(p.start), _month(p.end)))
    lanes: list[Lane] = []
    for src in sorted(by, key=lambda s: (source_label(s), s)):
        have = by[src]
        own = [m for m in months if min(have) <= m <= max(have)]
        lanes.append(
            Lane(
                label=source_label(src),
                covered=tuple(m in have for m in months),
                gaps=tuple(_label(m) for m in own if m not in have),
            )
        )
    n = len(periods)
    noun = "statement covers" if n == 1 else "statements cover"
    span = _label(months[0]) if len(months) == 1 else f"{_label(months[0])} to {_label(months[-1])}"
    return Coverage(
        months=tuple(_label(m) for m in months),
        lanes=tuple(lanes),
        statements=n,
        headline=f"{n} {noun} {span}.",
        gap_lines=tuple(
            f"No {lane.label} statement for {', '.join(lane.gaps)}." for lane in lanes if lane.gaps
        ),
    )


@dataclass(frozen=True)
class Tally:
    status: str  # match, inferred, mismatch
    count: int
    share: float  # percent of all checks, for the bar's width


def tally(checks: tuple[Check, ...]) -> tuple[Tally, ...]:
    """Counts per status, in the order a reader should look: mismatch first."""
    total = len(checks)
    out: list[Tally] = []
    for status in ("mismatch", "inferred", "match"):
        n = sum(c.status == status for c in checks)
        if n:
            out.append(Tally(status=status, count=n, share=n / total * 100))
    return tuple(out)


@dataclass(frozen=True)
class Twin:
    stated: float  # percent of the larger of the two, for the bar's width
    computed: float


def twin(stated: Qty, computed: Qty) -> Twin:
    """Stated units and units from trades on one scale: the larger is full."""
    top = max(stated.micros, computed.micros)
    if top == 0:
        return Twin(0.0, 0.0)
    return Twin(stated.micros / top * 100, computed.micros / top * 100)
