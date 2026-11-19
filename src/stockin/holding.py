"""One holding's own page: what you have, its weight against your target,
your verdict and thesis, which accounts hold it, and its tax dates and SIP
months.

Pure view model, like `story`: every number comes from the book, the price
book, `insights` and `thesis`. Nothing here writes a number of its own, so
the page and the dashboard always agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .book import Book
from .facts import Isin
from .insights import (
    CONCENTRATED_ONE,
    long_term_from,
    pct,
    rupees,
    short_name,
    sip_discipline,
    tenths_of,
    weights,
)
from .money import Money
from .prices import PriceBook
from .story import GROUP_SLOT, Month
from .thesis import BAND, Thesis, Verdict, for_book


@dataclass(frozen=True)
class AccountRow:
    account: str
    units: str
    value: str | None
    cost: str | None


@dataclass(frozen=True)
class LotRow:
    account: str
    bought: str | None  # None: bought before your statements start
    units: str
    cost: str | None
    long_term: str  # "long-term since 3 Jan 2026", "turns long-term on ...", ...


@dataclass(frozen=True)
class WeightBar:
    """Positions are percent of the bar's track, all on one scale."""

    weight: float
    line: float  # the 25% line
    band: tuple[float, float] | None  # your target, less and plus BAND
    target: float | None
    weight_text: str
    line_text: str
    band_text: str


@dataclass(frozen=True)
class HoldingPage:
    isin: Isin
    name: str
    full_name: str
    group: str
    slot: int
    value: str | None
    units: str
    cost: str | None
    gain: str | None
    price: str | None  # "₹1,130 on 2026-11-17"
    bar: WeightBar | None
    verdict: Verdict
    thesis: Thesis | None
    accounts: tuple[AccountRow, ...]
    lots: tuple[LotRow, ...]
    lots_note: str
    sip: tuple[Month, ...]


def price_text(micro_rupees: int) -> str:
    """Exact, at least two decimals: ₹1,367.00, ₹2,378.851, ₹94.4817."""
    whole, micros = divmod(micro_rupees, 1_000_000)
    frac = f"{micros:06d}".rstrip("0").ljust(2, "0")
    return "₹" + str(Money(paise=whole * 100))[:-3] + "." + frac


def _day(d: date) -> str:
    return f"{d.day} {d.strftime('%b %Y')}"


def _bar(w: int, thesis: Thesis | None) -> WeightBar:
    target = thesis.target if thesis else None
    ends = [w, CONCENTRATED_ONE]
    if target is not None:
        ends.append(target + BAND)
    top = max(ends) * 11 // 10 or 1  # a little room past the furthest mark

    def at(bps: int) -> float:
        return min(max(bps, 0), top) / top * 100

    band = None
    band_text = ""
    if target is not None:
        band = (at(target - BAND), at(target + BAND))
        lo, hi = (
            pct(tenths_of(max(target - BAND, 0), 10_000)),
            pct(tenths_of(target + BAND, 10_000)),
        )
        band_text = f"your target {pct(tenths_of(target, 10_000))}, hold between {lo} and {hi}"
    return WeightBar(
        weight=at(w),
        line=at(CONCENTRATED_ONE),
        band=band,
        target=at(target) if target is not None else None,
        weight_text=pct(tenths_of(w, 10_000)),
        line_text=f"{pct(tenths_of(CONCENTRATED_ONE, 10_000))} line",
        band_text=band_text,
    )


def holding_page(
    book: Book, prices: PriceBook, theses: dict[Isin, Thesis], isin: Isin, today: date
) -> HoldingPage | None:
    """The page for `isin`, or None when you do not hold it."""
    held = [h for h in book.holdings() if h.isin == isin]
    if not held:
        return None
    pos = next(p for p in book.positions() if p.isin == isin)
    inst = book.instrument(isin)
    ws, _, _ = weights(book, prices)
    w = next((x for x in ws if x.isin == isin), None)
    verdict = next(v for v in for_book(book, prices, theses) if v.isin == isin)
    thesis = theses.get(isin)
    quote = prices.get(isin)
    value = prices.value(isin, pos.qty)
    gain = None
    if value is not None and pos.cost is not None and pos.cost.paise > 0:
        g = value - pos.cost
        gain = f"{rupees(g)} ({pct(tenths_of(g.paise, pos.cost.paise))})"

    accounts: list[AccountRow] = []
    lots: list[LotRow] = []
    equity = inst is not None and inst.asset_class == "equity"
    for h in held:
        acct = book.account(h.account)
        label = acct.masked if acct else h.account
        hv = prices.value(isin, h.qty)
        accounts.append(
            AccountRow(
                account=label,
                units=str(h.qty),
                value=rupees(hv) if hv is not None else None,
                cost=rupees(h.cost) if h.cost is not None else None,
            )
        )
        for lot in book.timeline(h.account, isin).lots:
            if lot.cost is None:
                lots.append(LotRow(label, None, str(lot.qty), None, "date unknown"))
                continue
            lt = long_term_from(lot.acquired_on)
            if not equity:
                when = "not equity: different tax rules"
            elif lt <= today:
                when = f"long-term since {_day(lt)}"
            else:
                when = f"turns long-term on {_day(lt)}, in {(lt - today).days} days"
            lots.append(LotRow(label, _day(lot.acquired_on), str(lot.qty), rupees(lot.cost), when))
    note = (
        "Units bought before your statements start have no known date or cost, "
        "so no tax date is claimed for them."
        if any(r.bought is None for r in lots)
        else ""
    )

    sip_ins = sip_discipline(book)
    sip = tuple(
        Month(label=p.label, paid=p.value > 0)
        for p in (sip_ins.series if sip_ins else ())
        if p.key.endswith(f":{isin}")
    )
    group = w.group if w else ""
    return HoldingPage(
        isin=isin,
        name=short_name(inst.name) if inst else isin,
        full_name=inst.name if inst else isin,
        group=group,
        slot=GROUP_SLOT.get(group, 0),
        value=rupees(value) if value is not None else None,
        units=str(pos.qty),
        cost=rupees(pos.cost) if pos.cost is not None else None,
        gain=gain,
        price=f"{price_text(quote.price.micro_rupees)} on {quote.on.isoformat()}"
        if quote
        else None,
        bar=_bar(w.bps, thesis) if w else None,
        verdict=verdict,
        thesis=thesis,
        accounts=tuple(accounts),
        lots=tuple(lots),
        lots_note=note,
        sip=sip,
    )
