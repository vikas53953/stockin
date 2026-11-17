"""Numbers into learnings: the insight engine behind the story-first home.

Every insight is a pure function of (Book, PriceBook, today). It returns the
numbers first and builds its sentence only from those numbers, so the words
and the chart beside them cannot disagree (tests re-derive the sentence from
`numbers`). No model writes a number. Stockin is read-only: a sentence may
explain or suggest, never instruct a trade.

Percentages are basis points (Bps) and money is paise throughout; the only
rounding is `div_half_even`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Literal

from .book import Book
from .facts import Buy, Coverage, Income, Isin, Sell, Txn
from .money import Bps, Money, Qty, Value, div_half_even
from .prices import PriceBook

Kind = Literal[
    "what_changed", "concentration", "cost_coverage", "sip_discipline", "tax_clock", "invested"
]
Tone = Literal["good", "warn", "info", "unknown"]

CONCENTRATED_ONE: Bps = Bps(2500)
"""One holding above 25% of priced value is concentrated (rule of thumb seen
in Zerodha Console's threshold lines)."""
CONCENTRATED_TOP3: Bps = Bps(5000)


class Point(Value):
    """One mark on an insight's chart. `value` is paise for money series and
    Bps for share series; the insight's `unit` says which."""

    label: str
    value: int
    group: str = ""
    key: str = ""  # ISIN or period, for linking


class Insight(Value):
    kind: Kind
    tone: Tone
    headline: str
    detail: str
    numbers: dict[str, int | str]
    unit: Literal["paise", "bps", "count", "none"] = "none"
    series: tuple[Point, ...] = ()


class Weight(Value):
    isin: Isin
    name: str
    group: str  # "Equity funds", "Stocks", ...
    value: Money
    bps: Bps
    cost: Money | None


# ----------------------------------------------------------------- helpers


def pct(bps: int) -> str:
    """Bps to a percent with one decimal, half-even: 8183 -> '81.8%'."""
    tenths = div_half_even(bps, 10)
    sign = "-" if tenths < 0 else ""
    whole, frac = divmod(abs(tenths), 10)
    return f"{sign}{whole}.{frac}%"


def rupees(m: Money) -> str:
    """'₹15,000' when whole rupees, else '₹2,726.72'."""
    text = str(m)
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    if text.endswith(".00"):
        text = text[:-3]
    return f"{sign}₹{text}"


def _group(kind: str, asset_class: str) -> str:
    if kind == "stock":
        return "Stocks"
    if kind == "etf":
        return "ETFs"
    if asset_class == "debt":
        return "Debt funds"
    if asset_class == "gold":
        return "Gold"
    return "Equity funds"


def _short_name(name: str) -> str:
    for cut in (" - Direct", " Direct Plan", " - Regular", " Fund -", " LIMITED", " LTD"):
        i = name.find(cut)
        if i > 0:
            name = name[:i]
    return name.strip()


def weights(book: Book, prices: PriceBook) -> tuple[tuple[Weight, ...], Money, tuple[Isin, ...]]:
    """Priced positions by value, largest first; total priced value; and the
    ISINs held with no price. Ties order by ISIN, so input order never matters."""
    rows: list[tuple[Isin, str, str, Money, Money | None]] = []
    unpriced: list[Isin] = []
    for pos in book.positions():
        value = prices.value(pos.isin, pos.qty)
        if value is None:
            unpriced.append(pos.isin)
            continue
        inst = book.instrument(pos.isin)
        name = _short_name(inst.name) if inst else pos.isin
        kind = inst.kind if inst else "stock"
        if inst and inst.kind == "mutual_fund" and "ETF" in inst.name.upper():
            kind = "etf"
        group = _group(kind, inst.asset_class if inst else "equity")
        rows.append((pos.isin, name, group, value, pos.cost))
    total = Money(paise=sum(r[3].paise for r in rows))
    rows.sort(key=lambda r: (-r[3].paise, r[0]))
    out = tuple(
        Weight(
            isin=i,
            name=n,
            group=g,
            value=v,
            bps=v.share_of(total) if total.paise > 0 else Bps(0),
            cost=c,
        )
        for i, n, g, v, c in rows
    )
    return out, total, tuple(sorted(unpriced))


# ---------------------------------------------------------------- insights


def concentration(book: Book, prices: PriceBook) -> Insight | None:
    ws, total, _ = weights(book, prices)
    if not ws or total.paise <= 0:
        return None
    top = ws[0]
    top3 = Bps(div_half_even(sum(w.value.paise for w in ws[:3]) * 10_000, total.paise))
    hot = top.bps > CONCENTRATED_ONE or top3 > CONCENTRATED_TOP3
    if top.bps > CONCENTRATED_ONE:
        headline = f"One holding is {pct(top.bps)} of your money."
    else:
        headline = f"Your largest holding is {pct(top.bps)} of your money."
    drop = div_half_even(top.bps, 10)  # a 10% fall in it moves the whole by bps/10
    detail = (
        f"{top.name}. Your top 3 holdings are {pct(top3)} together. "
        f"If {top.name} falls 10%, your whole portfolio falls about {pct(drop)}. "
        "A common rule of thumb is no more than 25% in one holding."
    )
    return Insight(
        kind="concentration",
        tone="warn" if hot else "good",
        headline=headline,
        detail=detail,
        numbers={
            "top_bps": top.bps,
            "top3_bps": top3,
            "fall10_bps": drop,
            "top_isin": top.isin,
            "total_paise": total.paise,
        },
        unit="paise",
        series=tuple(
            Point(label=w.name, value=w.value.paise, group=w.group, key=w.isin) for w in ws
        ),
    )


def cost_coverage(book: Book, prices: PriceBook) -> Insight | None:
    ws, total, _ = weights(book, prices)
    if not ws or total.paise <= 0:
        return None
    known = [w for w in ws if w.cost is not None]
    known_value = Money(paise=sum(w.value.paise for w in known))
    known_cost = Money(paise=sum(w.cost.paise for w in known if w.cost is not None))
    gain = known_value - known_cost
    unknown_bps = Bps(10_000 - known_value.share_of(total)) if known else Bps(10_000)
    gain_bps = Bps(div_half_even(gain.paise * 10_000, known_cost.paise)) if known_cost.paise else 0
    numbers: dict[str, int | str] = {
        "unknown_bps": unknown_bps,
        "known_value_paise": known_value.paise,
        "known_cost_paise": known_cost.paise,
        "gain_paise": gain.paise,
        "gain_bps": gain_bps,
    }
    word = "up" if gain.paise >= 0 else "down"
    if unknown_bps > 0:
        headline = f"Gain is unknown for {pct(unknown_bps)} of your money."
        detail = (
            "Your statements start after these holdings were bought, so their cost "
            "isn't known. A statement from your first investment fixes this."
        )
        if known:
            detail += (
                f" On the rest you're {word} {rupees(Money(paise=abs(gain.paise)))} "
                f"({pct(abs(gain_bps))})."
            )
        tone: Tone = "unknown"
    else:
        headline = (
            f"You're {word} {rupees(Money(paise=abs(gain.paise)))} "
            f"({pct(abs(gain_bps))}) on what you hold."
        )
        detail = "Value at the latest closing prices, minus what you paid (FIFO, with charges)."
        tone = "good" if gain.paise >= 0 else "warn"
    return Insight(
        kind="cost_coverage",
        tone=tone,
        headline=headline,
        detail=detail,
        numbers=numbers,
        unit="bps",
        series=(
            Point(label="cost known", value=10_000 - unknown_bps),
            Point(label="cost unknown", value=unknown_bps),
        ),
    )


def _txns(book: Book) -> list[Txn]:
    return [f for f in book.facts if isinstance(f, Txn)]


def _coverage_ends(book: Book) -> list[date]:
    return sorted({f.end for f in book.facts if isinstance(f, Coverage)})


def _month(d: date) -> tuple[int, int]:
    return (d.year, d.month)


def _months(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = a
    while (y, m) <= b:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def what_changed(book: Book) -> Insight | None:
    """Money in and out since the previous statement. The market's share of
    the change needs prices on both dates, which Stockin does not keep yet,
    so this insight says only what you did."""
    ends = _coverage_ends(book)
    txns = _txns(book)
    if not ends or not txns:
        return None
    end = ends[-1]
    start = ends[-2] + timedelta(days=1) if len(ends) > 1 else min(t.on for t in txns)
    window = [t for t in txns if start <= t.on <= end]
    added = sum(t.event.amount.paise for t in window if isinstance(t.event, Buy))
    out = sum(t.event.amount.paise for t in window if isinstance(t.event, Sell))
    income = sum(t.event.amount.paise for t in window if isinstance(t.event, Income))
    buys = sum(1 for t in window if isinstance(t.event, Buy))
    since = f"{start.day} {_MON[start.month - 1]} {start.year}"
    until = f"{end.day} {_MON[end.month - 1]} {end.year}"
    headline = f"From {since} to {until}: you added {rupees(Money(paise=added))}" + (
        f" and took out {rupees(Money(paise=out))}." if out else "."
    )
    detail = f"{buys} purchase{'s' if buys != 1 else ''}"
    if income:
        detail += f" and {rupees(Money(paise=income))} paid out as dividends"
    detail += (
        ". How much the market moved your money needs prices on both dates; "
        "that arrives when Stockin keeps price history."
    )
    return Insight(
        kind="what_changed",
        tone="info",
        headline=headline,
        detail=detail,
        numbers={
            "start": start.isoformat(),
            "end": end.isoformat(),
            "added_paise": added,
            "out_paise": out,
            "income_paise": income,
            "buys": buys,
        },
        unit="paise",
        series=(
            Point(label="added", value=added),
            Point(label="taken out", value=-out),
        ),
    )


def sip_discipline(book: Book) -> Insight | None:
    """A SIP is a (account, ISIN) with purchases in at least 3 different months.
    Expected months run from its first purchase month to the month of the
    latest statement; each month with a purchase is paid."""
    ends = _coverage_ends(book)
    if not ends:
        return None
    last = _month(ends[-1])
    by_pair: dict[tuple[str, str], set[tuple[int, int]]] = defaultdict(set)
    for t in _txns(book):
        if isinstance(t.event, Buy):
            by_pair[(t.account, t.isin)].add(_month(t.on))
    sips = {p: ms for p, ms in by_pair.items() if len(ms) >= 3}
    if not sips:
        return None
    expected = paid = 0
    series: list[Point] = []
    for (acct, isin), months in sorted(sips.items()):
        inst = book.instrument(Isin(isin))
        name = _short_name(inst.name) if inst else isin
        for ym in _months(min(months), last):
            expected += 1
            ok = ym in months
            paid += ok
            series.append(
                Point(
                    label=f"{_MON[ym[1] - 1]} {ym[0]}",
                    value=1 if ok else 0,
                    group=name,
                    key=f"{acct}:{isin}",
                )
            )
    missed = expected - paid
    n = len(sips)
    headline = (
        f"All {expected} SIP instalments paid across {n} SIP{'s' if n != 1 else ''}."
        if not missed
        else f"{missed} of {expected} SIP instalments missed across {n} SIP{'s' if n != 1 else ''}."
    )
    detail = (
        "Paying every month means you buy more units when prices are low."
        if not missed
        else "A missed month is a month you didn't buy, often when prices were lower."
    )
    return Insight(
        kind="sip_discipline",
        tone="good" if not missed else "warn",
        headline=headline,
        detail=detail,
        numbers={"expected": expected, "paid": paid, "missed": missed, "sips": n},
        unit="count",
        series=tuple(series),
    )


def long_term_from(acquired: date) -> date:
    """Equity is long-term when held more than 12 months: from the day after
    the first anniversary (29 Feb rolls to 1 Mar)."""
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:
        anniversary = date(acquired.year + 1, 3, 1)
    return anniversary + timedelta(days=1)


def tax_clock(book: Book, today: date) -> Insight | None:
    """The next equity lots to turn long-term after `today`."""
    upcoming: dict[date, list[tuple[str, Qty, Money]]] = defaultdict(list)
    for h in book.holdings():
        inst = book.instrument(h.isin)
        if inst is None or inst.asset_class != "equity":
            continue
        for lot in book.timeline(h.account, h.isin).lots:
            if lot.cost is None:
                # bought before your statements start: the real purchase
                # date is unknown, so no date is claimed for it
                continue
            lt = long_term_from(lot.acquired_on)
            if lt > today:
                upcoming[lt].append((_short_name(inst.name), lot.qty, lot.cost))
    if not upcoming:
        return None
    when = min(upcoming)
    lots = upcoming[when]
    names = sorted({n for n, _, _ in lots})
    cost = Money(paise=sum(c.paise for _, _, c in lots))
    what = f"{rupees(cost)} (at cost) of "
    on = f"{when.day} {_MON[when.month - 1]} {when.year}"
    headline = f"{what}{' and '.join(names)} turn long-term on {on}."
    days = (when - today).days
    detail = (
        f"That's in {days} days. Selling before then is taxed as short-term gains. "
        "Worked out from your purchase dates, oldest first. Units bought before your "
        "statements start are left out, because their purchase date isn't known."
    )
    return Insight(
        kind="tax_clock",
        tone="info",
        headline=headline,
        detail=detail,
        numbers={
            "date": when.isoformat(),
            "days": days,
            "cost_paise": cost.paise,
            "lots": len(lots),
        },
        unit="paise",
        series=tuple(
            Point(label=f"{d.day} {_MON[d.month - 1]} {d.year}", value=len(v), key=d.isoformat())
            for d, v in sorted(upcoming.items())[:12]
        ),
    )


def _fy(d: date) -> int:
    """FY start year: 15 Jan 2026 is FY 2025-26 -> 2025."""
    return d.year if d.month >= 4 else d.year - 1


def invested(book: Book) -> Insight | None:
    net: dict[int, int] = defaultdict(int)
    for t in _txns(book):
        if isinstance(t.event, Buy):
            net[_fy(t.on)] += t.event.amount.paise
        elif isinstance(t.event, Sell):
            net[_fy(t.on)] -= t.event.amount.paise
    if not net:
        return None
    fy = max(net)
    label = f"FY {fy}-{(fy + 1) % 100:02d}"
    amount = Money(paise=net[fy])
    verb = "invested" if amount.paise >= 0 else "took out"
    return Insight(
        kind="invested",
        tone="info",
        headline=f"You {verb} {rupees(Money(paise=abs(amount.paise)))} (net) in {label}.",
        detail=(
            "Purchases minus redemptions in your statements, by financial year (April to March)."
        ),
        numbers={"fy": label, "net_paise": amount.paise},
        unit="paise",
        series=tuple(
            Point(label=f"FY {y}-{(y + 1) % 100:02d}", value=v, key=str(y))
            for y, v in sorted(net.items())
        ),
    )


def all_insights(book: Book, prices: PriceBook, today: date) -> tuple[Insight, ...]:
    """The home screen's cards, in reading order: what changed, then what
    needs attention, then the rest."""
    found = [
        what_changed(book),
        concentration(book, prices),
        cost_coverage(book, prices),
        sip_discipline(book),
        tax_clock(book, today),
        invested(book),
    ]
    present = [i for i in found if i is not None]
    first = [i for i in present if i.kind == "what_changed"]
    rest = [i for i in present if i.kind != "what_changed"]
    rest.sort(key=lambda i: {"warn": 0, "unknown": 1, "good": 2, "info": 3}[i.tone])
    return tuple(first + rest)
