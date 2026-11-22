"""What the Net worth screen draws, worked out before the template sees it.

The template only places strings: every number, width and sentence here is
computed from one `NetWorth`, so a chart and the sentence beside it cannot
disagree. Widths are exact hundredths of a percent built from basis points
with integer arithmetic; no float touches a number the owner reads.
"""

from __future__ import annotations

from dataclasses import dataclass

from .insights import pct, tenths_of
from .money import Money, div_half_even
from .networth import (
    BUCKET_NAME,
    BUCKETS,
    KIND_NAME,
    KINDS,
    STALE_DAYS,
    Bucket,
    Kind,
    Line,
    NetWorth,
)

SERIES: dict[Bucket, int] = {
    "equity": 1,
    "cash": 2,
    "debt": 3,
    "gold": 4,
    "retirement": 5,
    "real_estate": 0,
    "other": 0,
}
"""Each class keeps one series colour from tokens.css (s0..s5, a fixed
palette of six: real estate and other share the neutral s0)."""

SLOT: dict[Bucket, str] = {b: f"s{n}" for b, n in SERIES.items()}


def width(hundredths: int) -> str:
    """Hundredths of a percent to a CSS percentage, exactly: 1234 -> '12.34'."""
    hundredths = max(0, min(10_000, hundredths))
    return f"{hundredths // 100}.{hundredths % 100:02d}"


def rupees(m: Money) -> str:
    return f"₹{m}"


def short_rupees(m: Money) -> str:
    """₹ in lakh or crore with two decimals for a tile, exact by half-even:
    1,07,29,128.48 -> '₹1.07 Cr'; 2,50,000 -> '₹2.50 L'; below a lakh as is."""
    p = m.paise
    sign = "-" if p < 0 else ""
    p = abs(p)
    if p >= 100 * 10**7:
        h = div_half_even(p, 10**7)  # hundredths of a crore
        return f"{sign}₹{h // 100}.{h % 100:02d} Cr"
    if p >= 100 * 10**5:
        h = div_half_even(p, 10**5)  # hundredths of a lakh
        return f"{sign}₹{h // 100}.{h % 100:02d} L"
    return f"{sign}₹{Money(paise=p)}"


@dataclass(frozen=True)
class Segment:
    bucket: str
    name: str
    slot: str
    width: str  # of the whole bar, percent
    share: str  # the same share as the sentences print it
    tenths: int  # that share in tenths of a percent, for the shared macros
    series: int
    value: str


@dataclass(frozen=True)
class ClassBar:
    name: str
    slot: str
    series: int
    tenths: int  # weight, tenths of a percent, as the sentence prints it
    target_tenths: int | None
    share: str
    target_text: str | None
    drift_text: str | None
    tone: str  # warn | good | info


@dataclass(frozen=True)
class Tile:
    kind: str
    kind_name: str
    label: str
    value: str
    short: str
    as_of: str | None
    pill: str | None  # "matured", "N days old" or None
    pill_tone: str | None
    closable: bool


@dataclass(frozen=True)
class Group:
    name: str
    slot: str
    total: str
    tiles: tuple[Tile, ...]


@dataclass(frozen=True)
class Card:
    tone: str
    badge: str
    text: str


@dataclass(frozen=True)
class Page:
    on: str
    total: str
    total_short: str
    owned: str
    owed: str
    own_width: str  # own : owe as parts of (own + owe), for the split bar
    owe_width: str
    owed_share: str | None
    negative: bool
    segments: tuple[Segment, ...]
    mix_parts: tuple[tuple[str, float, int, str], ...]  # ui.seg: (name, width, series, text)
    donut: str  # CSS conic-gradient of the same segments, exact hundredths
    bars: tuple[ClassBar, ...]
    cards: tuple[Card, ...]
    groups: tuple[Group, ...]
    has_targets: bool
    target_inputs: tuple[tuple[str, str, str], ...]  # (bucket, name, current percent text)
    kinds: tuple[tuple[str, str], ...]
    empty: bool


_BADGE = {
    "warn": "▲ look at this",
    "good": "✓ on track",
    "info": "• for your information",
    "unknown": "? unknown",
}


def _tile(ln: Line) -> Tile:
    pill: str | None = None
    tone: str | None = None
    if ln.matured is not None:
        pill, tone = f"matured {ln.matured.isoformat()}", "warn"
    elif (
        ln.kind in ("bank", "epf", "ppf", "nps", "loan")
        and ln.days_old is not None
        and ln.days_old > STALE_DAYS
    ):
        pill, tone = f"{ln.days_old} days old", "warn"
    kind: Kind | None = ln.kind if ln.kind != "market" else None
    return Tile(
        kind=ln.kind,
        kind_name=KIND_NAME[kind] if kind else "Market",
        label=ln.label,
        value=rupees(ln.value),
        short=short_rupees(ln.value),
        as_of=ln.as_of.isoformat() if ln.as_of else None,
        pill=pill,
        pill_tone=tone,
        closable=kind is not None,
    )


def page(nw: NetWorth, priced_on: str | None = None) -> Page:
    owned = nw.owned.paise
    segments = tuple(
        Segment(
            bucket=s.bucket,
            name=s.name,
            slot=SLOT[s.bucket],
            width=width(s.bps),
            share=pct(tenths_of(s.bps, 10_000)),
            tenths=tenths_of(s.bps, 10_000),
            series=SERIES[s.bucket],
            value=rupees(s.value),
        )
        for s in nw.slices
        if s.bps > 0
    )
    stops: list[str] = []
    at = 0
    for s in nw.slices:
        if s.bps > 0:
            stops.append(f"var(--s{SERIES[s.bucket]}) {width(at)}% {width(at + s.bps)}%")
            at += s.bps
    donut = f"conic-gradient({', '.join(stops)})" if stops else "none"
    bars: list[ClassBar] = []
    drift_buckets = {n.bucket for n in nw.notes if n.kind == "drift"}
    for s in nw.slices:
        drift_text = None
        if s.target is not None and s.drift is not None:
            t = tenths_of(s.bps, 10_000) - tenths_of(s.target, 10_000)
            sign = "+" if t > 0 else ("-" if t < 0 else "±")
            whole, frac = divmod(abs(t), 10)
            drift_text = f"{sign}{whole}.{frac} pts"
        bars.append(
            ClassBar(
                name=s.name,
                slot=SLOT[s.bucket],
                series=SERIES[s.bucket],
                tenths=tenths_of(s.bps, 10_000),
                target_tenths=tenths_of(s.target, 10_000) if s.target is not None else None,
                share=pct(tenths_of(s.bps, 10_000)),
                target_text=pct(tenths_of(s.target, 10_000)) if s.target is not None else None,
                drift_text=drift_text,
                tone="warn"
                if s.bucket in drift_buckets
                else ("good" if s.target is not None else "info"),
            )
        )
    groups: list[Group] = []
    for b in BUCKETS:
        lines = [ln for ln in nw.lines if ln.bucket == b]
        if lines:
            groups.append(
                Group(
                    name=BUCKET_NAME[b],
                    slot=SLOT[b],
                    total=rupees(Money(paise=sum(ln.value.paise for ln in lines))),
                    tiles=tuple(_tile(ln) for ln in lines),
                )
            )
    loans = [ln for ln in nw.lines if ln.bucket is None]
    if loans:
        groups.append(
            Group(
                name="Loans",
                slot="loan",
                total=rupees(nw.owed),
                tiles=tuple(_tile(ln) for ln in loans),
            )
        )
    both = owned + nw.owed.paise
    own_bps = div_half_even(owned * 10_000, both) if both > 0 else 0
    by = {s.bucket: s for s in nw.slices}
    return Page(
        on=nw.on.isoformat() + (f" · prices {priced_on}" if priced_on else ""),
        total=rupees(nw.total),
        total_short=short_rupees(nw.total),
        owned=rupees(nw.owned),
        owed=rupees(nw.owed),
        own_width=width(own_bps),
        owe_width=width(10_000 - own_bps if both > 0 else 0),
        owed_share=pct(tenths_of(nw.owed.paise, owned)) if owned > 0 and nw.owed.paise else None,
        negative=nw.total.paise < 0,
        segments=segments,
        # the float is only a bar width; the text is the share the sentences print
        mix_parts=tuple((seg.name, seg.tenths / 10, seg.series, seg.share) for seg in segments),
        donut=donut,
        bars=tuple(bars),
        cards=tuple(Card(tone=n.tone, badge=_BADGE[n.tone], text=n.text) for n in nw.notes),
        groups=tuple(groups),
        has_targets=bool(nw.targets),
        target_inputs=tuple(
            (
                b,
                BUCKET_NAME[b],
                _percent_text(nw.targets[b]) if b in nw.targets else "",
            )
            for b in BUCKETS
        ),
        kinds=tuple((k, KIND_NAME[k]) for k in KINDS),
        empty=not nw.lines and not by,
    )


def _percent_text(bps: int) -> str:
    whole, frac = divmod(bps, 100)
    return f"{whole}.{frac:02d}".rstrip("0").rstrip(".")
