"""The home screen's story (direction A, with B's map inside): what changed,
up to four insight cards, then a map of where the money is.

Pure view model: every number here comes from `insights`, which computes it
from the ledger and the price book. Nothing in this module writes a number
of its own; it only lays out what insights already said, so the sentence on
a card and the bars beside it are drawn from the same `Insight`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .insights import CONCENTRATED_ONE, Insight, Weight, pct, rupees, tenths_of
from .money import Money

MAX_CARDS = 4
"""One sentence per card and no more than four cards (direction A's rule)."""

MAX_BARS = 5

GROUP_SLOT = {
    "Equity funds": 1,
    "Stocks": 2,
    "ETFs": 3,
    "Debt funds": 4,
    "Gold": 5,
}
"""A fixed colour slot per kind of holding, so a kind keeps its colour
whatever else is held (colour follows the entity, never its rank)."""

LEARN = {
    "concentration": "Why 25% in one holding is a common limit: if it falls 10%, "
    "a quarter-weight holding alone takes 2.5% off everything.",
    "cost_coverage": "Gain needs the price you paid. A statement that starts after a "
    "purchase cannot show it, so Stockin leaves it unknown rather than guess.",
    "sip_discipline": "A SIP paid every month buys more units when prices are low "
    "and fewer when they are high.",
    "tax_clock": "Equity held more than a year is taxed as long-term; selling "
    "earlier is short-term. Stockin dates each lot from your purchases.",
    "invested": "What you put in each year, from your statements' purchases and redemptions.",
}


@dataclass(frozen=True)
class Bar:
    label: str
    value: str  # as printed beside the bar
    width: float  # percent of the card's bar track
    group: int  # colour slot, 0 for none


@dataclass(frozen=True)
class Month:
    label: str  # "Jan 2026"
    paid: bool


@dataclass(frozen=True)
class Streak:
    name: str
    months: tuple[Month, ...]


@dataclass(frozen=True)
class Card:
    kind: str
    tone: str
    headline: str
    detail: str
    learn: str
    bars: tuple[Bar, ...]
    line: float | None  # a threshold, in percent of the track (concentration)
    line_label: str
    streaks: tuple[Streak, ...] = ()  # SIPs: one row of months per SIP


@dataclass(frozen=True)
class Tile:
    name: str
    isin: str
    group: str
    slot: int
    share: str  # "82.1%"
    value: str  # "₹23,80,392.50"
    x: float  # percent of the map's width/height
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class Story:
    total: str | None
    holdings: int
    priced: int
    changed: Insight | None
    cards: tuple[Card, ...]
    tiles: tuple[Tile, ...]
    legend: tuple[tuple[str, int], ...]


def _bars(i: Insight) -> tuple[tuple[Bar, ...], float | None, str]:
    points = i.series[:MAX_BARS]
    if not points or i.unit not in ("paise", "bps", "count"):
        return (), None, ""
    top = max(max(p.value for p in points), 1)
    line: float | None = None
    label = ""
    total = i.numbers.get("total_paise")
    share_of_total = i.kind == "concentration" and isinstance(total, int) and total > 0
    if share_of_total:
        # bars as shares of the whole, with the 25% line on the same scale
        assert isinstance(total, int)
        limit = total * CONCENTRATED_ONE // 10_000
        top = max(top, limit)
        line = limit / top * 100
        label = f"{pct(tenths_of(CONCENTRATED_ONE, 10_000))} line"

    def shown(v: int) -> str:
        if share_of_total:
            assert isinstance(total, int)
            return pct(tenths_of(v, total))
        if i.unit == "bps":
            return pct(tenths_of(v, 10_000))
        if i.unit == "paise":
            return rupees(Money(paise=v))
        return str(v)

    bars = tuple(
        Bar(
            label=p.label,
            value=shown(p.value),
            width=max(p.value, 0) / top * 100,
            group=GROUP_SLOT.get(p.group, 0),
        )
        for p in points
    )
    return bars, line, label


def _streaks(i: Insight) -> tuple[Streak, ...]:
    """A SIP's months, paid or missed, in order, one row per SIP."""
    rows: dict[str, list[Month]] = {}
    names: dict[str, str] = {}
    for p in i.series:
        rows.setdefault(p.key, []).append(Month(label=p.label, paid=p.value > 0))
        names[p.key] = p.group
    return tuple(Streak(name=names[k], months=tuple(m)) for k, m in rows.items())


def card(i: Insight) -> Card:
    if i.kind == "sip_discipline":
        return Card(
            kind=i.kind,
            tone=i.tone,
            headline=i.headline,
            detail=i.detail,
            learn=LEARN.get(i.kind, ""),
            bars=(),
            line=None,
            line_label="",
            streaks=_streaks(i),
        )
    bars, line, label = _bars(i)
    return Card(
        kind=i.kind,
        tone=i.tone,
        headline=i.headline,
        detail=i.detail,
        learn=LEARN.get(i.kind, ""),
        bars=bars,
        line=line,
        line_label=label,
    )


# ---------------------------------------------------------------- treemap


def _worst(row: list[int], side: float, scale: float) -> float:
    """Squarify's measure: the worst aspect ratio of `row` laid along `side`."""
    s = sum(row) * scale
    if s <= 0 or side <= 0:
        return float("inf")
    biggest, smallest = max(row) * scale, min(row) * scale
    return max(side * side * biggest / (s * s), (s * s) / (side * side * smallest))


def squarify(
    values: list[int], width: float, height: float
) -> list[tuple[float, float, float, float]]:
    """Rectangles (x, y, w, h) filling width by height, one per value, each
    with area proportional to its value (Bruls, Huijsing and van Wijk's
    squarified treemap). `values` must be positive and sorted largest first."""
    total = sum(values)
    if total <= 0:
        return []
    scale = width * height / total
    out: list[tuple[float, float, float, float]] = []
    x, y, w, h = 0.0, 0.0, width, height
    i = 0
    while i < len(values):
        side = min(w, h)
        row = [values[i]]
        j = i + 1
        while j < len(values) and _worst([*row, values[j]], side, scale) <= _worst(
            row, side, scale
        ):
            row.append(values[j])
            j += 1
        # Each row takes its share of the box that is left, and each tile its
        # share of the row, so the rows partition the box exactly; deriving
        # them from areas instead drifts until a tiny last tile overflows.
        rest = sum(values[i:])
        part = sum(row)
        if w >= h:  # lay the row down the left edge
            col = w if j == len(values) else w * part / rest
            yy = y
            for k, v in enumerate(row):
                hh = (y + h - yy) if k == len(row) - 1 else h * v / part
                out.append((x, yy, col, hh))
                yy += hh
            x, w = x + col, w - col
        else:  # along the top edge
            rh = h if j == len(values) else h * part / rest
            xx = x
            for k, v in enumerate(row):
                ww = (x + w - xx) if k == len(row) - 1 else w * v / part
                out.append((xx, y, ww, rh))
                xx += ww
            y, h = y + rh, h - rh
        i = j
    return out


MAP_W, MAP_H = 100.0, 56.0
"""The map's box, in the same units as its CSS aspect ratio (100 / 56)."""


def tiles(weights: tuple[Weight, ...]) -> tuple[Tile, ...]:
    shown = [w for w in weights if w.value.paise > 0]
    rects = squarify([w.value.paise for w in shown], MAP_W, MAP_H)
    return tuple(
        Tile(
            name=w.name,
            isin=w.isin,
            group=w.group,
            slot=GROUP_SLOT.get(w.group, 0),
            share=pct(tenths_of(w.bps, 10_000)),
            value=rupees(w.value),
            x=x / MAP_W * 100,
            y=y / MAP_H * 100,
            w=ww / MAP_W * 100,
            h=hh / MAP_H * 100,
        )
        for w, (x, y, ww, hh) in zip(shown, rects, strict=True)
    )


def story(
    insights: tuple[Insight, ...],
    weights: tuple[Weight, ...],
    total_paise: int,
    holdings: int,
) -> Story:
    changed = next((i for i in insights if i.kind == "what_changed"), None)
    rest = [i for i in insights if i.kind != "what_changed"][:MAX_CARDS]
    groups = sorted({w.group for w in weights}, key=lambda g: GROUP_SLOT.get(g, 99))
    return Story(
        total=rupees(Money(paise=total_paise)) if weights else None,
        holdings=holdings,
        priced=len(weights),
        changed=changed,
        cards=tuple(card(i) for i in rest),
        tiles=tiles(weights),
        legend=tuple((g, GROUP_SLOT.get(g, 0)) for g in groups),
    )
