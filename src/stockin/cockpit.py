"""The home screen as a cockpit (redesign direction A): the numbers each tile
draws, laid out from objects other modules already computed.

Pure view model. Every figure here is read from an `Insight`, a `Weight` or
a `Verdict`; this module only turns basis points into widths for a chart
and reuses the same text the sentences print, so a tile's label and the
sentence beside it can never disagree. Nothing here writes a number of its
own, and nothing here can place an order.
"""

from __future__ import annotations

from dataclasses import dataclass

from .insights import Insight, Weight, pct, rupees, tenths_of
from .money import Money
from .story import GROUP_SLOT
from .thesis import BAND, Verdict

DECIDE_ORDER = ("exit", "trim", "add", "hold")
"""Calls shown as gauges on the Decide tile, most urgent first. `review`
(no thesis yet) is listed once as a count, not as a row each."""

MAX_DECIDE = 4
MAX_TOP = 6


@dataclass(frozen=True)
class FlowRow:
    label: str
    amount: str  # the same text the "what changed" sentence prints
    width: float  # percent of the larger flow
    way: str  # "in" or "out"


@dataclass(frozen=True)
class MixPart:
    group: str
    share: float  # percent, for the bar's width
    text: str  # "84.1%", rounded once from the same basis points
    slot: int


@dataclass(frozen=True)
class Decide:
    isin: str
    name: str
    call: str
    because: str  # the verdict's own sentence
    weight: float  # percent
    target: float | None  # percent
    band: float  # percent either side of the target
    weight_text: str
    target_text: str


@dataclass(frozen=True)
class TopRow:
    isin: str
    name: str
    group: str
    slot: int
    share: float
    text: str
    call: str


@dataclass(frozen=True)
class Cockpit:
    flows: tuple[FlowRow, ...]
    mix: tuple[MixPart, ...]
    decide: tuple[Decide, ...]
    more_decide: int  # actionable calls beyond MAX_DECIDE
    review: tuple[tuple[str, str], ...]  # (isin, name) with no thesis yet
    top: tuple[TopRow, ...]
    known: float | None  # percent of priced value whose cost is known
    known_text: str
    unknown_text: str


def _bps_pct(bps: int) -> float:
    return bps / 100


def flows(changed: Insight | None) -> tuple[FlowRow, ...]:
    """Money in and out over the statements' window, as two bars on one scale."""
    if changed is None:
        return ()
    added = changed.numbers.get("added_paise", 0)
    out = changed.numbers.get("out_paise", 0)
    assert isinstance(added, int) and isinstance(out, int)
    top = max(added, out)
    if top <= 0:
        return ()
    rows = [("Added", added, "in"), ("Took out", out, "out")]
    return tuple(
        FlowRow(label=label, amount=rupees(Money(paise=v)), width=v / top * 100, way=way)
        for label, v, way in rows
        if v > 0
    )


def mix(weights: tuple[Weight, ...]) -> tuple[MixPart, ...]:
    """Share of priced value per kind of holding, in the colour order of the map."""
    by: dict[str, int] = {}
    for w in weights:
        by[w.group] = by.get(w.group, 0) + w.value.paise
    total = sum(by.values())
    if total <= 0:
        return ()
    groups = sorted(by, key=lambda g: (GROUP_SLOT.get(g, 99), g))
    return tuple(
        MixPart(
            group=g,
            share=by[g] / total * 100,
            text=pct(tenths_of(by[g], total)),
            slot=GROUP_SLOT.get(g, 0),
        )
        for g in groups
    )


def decide(verdicts: tuple[Verdict, ...]) -> tuple[tuple[Decide, ...], int]:
    rows = [v for v in verdicts if v.call in DECIDE_ORDER and v.weight is not None]
    rows.sort(key=lambda v: (DECIDE_ORDER.index(v.call), -(v.weight or 0), v.isin))
    shown = tuple(
        Decide(
            isin=v.isin,
            name=v.name,
            call=v.call,
            because=v.because,
            weight=_bps_pct(v.weight or 0),
            target=_bps_pct(v.target) if v.target is not None else None,
            band=_bps_pct(BAND),
            # the sentence's own text; an exit sentence prints no weight
            weight_text=v.weight_text or pct(tenths_of(v.weight or 0, 10_000)),
            target_text=v.target_text
            or (pct(tenths_of(v.target, 10_000)) if v.target is not None else ""),
        )
        for v in rows[:MAX_DECIDE]
    )
    return shown, max(len(rows) - MAX_DECIDE, 0)


def known(cost: Insight | None) -> tuple[float | None, str, str]:
    """Percent of priced value with a known cost, from the cost insight's own
    tenths, so the bar's legend repeats the headline's number exactly."""
    if cost is None:
        return None, "", ""
    unknown_t = cost.numbers.get("unknown_tenths")
    if not isinstance(unknown_t, int):
        return None, "", ""
    return (1000 - unknown_t) / 10, pct(1000 - unknown_t), pct(unknown_t)


def cockpit(
    insights: tuple[Insight, ...],
    weights: tuple[Weight, ...],
    verdicts: tuple[Verdict, ...],
) -> Cockpit:
    by_kind = {i.kind: i for i in insights}
    calls = {v.isin: v.call for v in verdicts}
    shown, more = decide(verdicts)
    k, k_text, u_text = known(by_kind.get("cost_coverage"))
    return Cockpit(
        flows=flows(by_kind.get("what_changed")),
        mix=mix(weights),
        decide=shown,
        more_decide=more,
        review=tuple((v.isin, v.name) for v in verdicts if v.call == "review"),
        top=tuple(
            TopRow(
                isin=w.isin,
                name=w.name,
                group=w.group,
                slot=GROUP_SLOT.get(w.group, 0),
                share=_bps_pct(w.bps),
                text=pct(tenths_of(w.bps, 10_000)),
                call=calls.get(w.isin, "review"),
            )
            for w in weights[:MAX_TOP]
        ),
        known=k,
        known_text=k_text,
        unknown_text=u_text,
    )
