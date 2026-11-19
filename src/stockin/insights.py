"""Numbers into learnings: the insight engine behind the story-first home.

Every insight is a pure function of (Book, PriceBook, today). It returns the
numbers first and builds its sentence only from those numbers, so the words
and the chart beside them cannot disagree (tests re-derive the sentence from
`numbers`). No model writes a number. Stockin is read-only: a sentence may
explain or suggest, never instruct a trade.

Money is paise. A percentage in a sentence is tenths of a percent, rounded
once (half-even) from the paise it comes from, so "97.652%" prints 97.7%,
never a rounded-twice 97.6%. Chart series carry basis points or paise. The
only rounding is `div_half_even`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Literal

from .book import Book
from .facts import AccountId, Buy, Coverage, Income, Isin, Sell, Txn
from .money import Bps, Money, Value, div_half_even
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


def tenths_of(part: int, whole: int) -> int:
    """part / whole in tenths of a percent, rounded once, half-even."""
    return div_half_even(part * 1_000, whole)


def pct(tenths: int) -> str:
    """Tenths of a percent to text: 818 -> '81.8%'."""
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


def short_name(name: str) -> str:
    for cut in (
        " - Direct",
        " Direct Plan",
        " - Regular",
        " Fund -",
        " - EQ",
        " LIMITED",
        " LTD",
    ):
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
        name = short_name(inst.name) if inst else pos.isin
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


def _names(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _unpriced_note(book: Book, unpriced: tuple[Isin, ...]) -> str:
    if not unpriced:
        return ""
    names: list[str] = []
    for i in unpriced:
        inst = book.instrument(i)
        names.append(short_name(inst.name) if inst else i)
    return (
        f" {_plural(len(unpriced), 'holding')} ({_names(sorted(names))}) "
        f"{'has' if len(unpriced) == 1 else 'have'} no price yet and "
        f"{'is' if len(unpriced) == 1 else 'are'} left out."
    )


# ---------------------------------------------------------------- insights


def concentration(book: Book, prices: PriceBook) -> Insight | None:
    """Largest holding and top 3 as shares of the priced value. Holdings with
    no price are named and left out, and the sentence says "priced"."""
    ws, total, unpriced = weights(book, prices)
    if not ws or total.paise <= 0:
        return None
    top = ws[0]
    top_t = tenths_of(top.value.paise, total.paise)
    top3_value = sum(w.value.paise for w in ws[:3])
    top3_t = tenths_of(top3_value, total.paise)
    fall10_t = tenths_of(top.value.paise, total.paise * 10)  # 10% of its share
    over_one = top.value.paise * 10_000 > CONCENTRATED_ONE * total.paise
    over_three = len(ws) > 3 and top3_value * 10_000 > CONCENTRATED_TOP3 * total.paise
    of = "of what's priced" if unpriced else "of your money"
    alone = len(ws) < 2 or ws[1].value.paise < top.value.paise
    if over_one and alone:
        headline = f"One holding is {pct(top_t)} {of}."
    else:
        headline = f"Your largest holding is {pct(top_t)} {of}."
    priced = " priced" if unpriced else ""
    whole = "what's priced" if unpriced else "your whole portfolio"
    group = f"Your top 3{priced} holdings are {pct(top3_t)} together. " if len(ws) > 3 else ""
    detail = (
        f"{top.name}. {group}"
        f"If {top.name} falls 10%, {whole} falls about {pct(fall10_t)}. "
        "A common rule of thumb is no more than 25% in one holding."
        + _unpriced_note(book, unpriced)
    )
    return Insight(
        kind="concentration",
        tone="warn" if over_one or over_three else "good",
        headline=headline,
        detail=detail,
        numbers={
            "top_tenths": top_t,
            "top3_tenths": top3_t,
            "fall10_tenths": fall10_t,
            "top_isin": top.isin,
            "top_paise": top.value.paise,
            "total_paise": total.paise,
            "holdings": len(ws),
            "unpriced": len(unpriced),
        },
        unit="paise",
        series=tuple(
            Point(label=w.name, value=w.value.paise, group=w.group, key=w.isin) for w in ws
        ),
    )


def _gain_words(gain: Money, cost: Money) -> tuple[str, int | None]:
    """('up ₹2,726.72 (5.5%)', tenths) or ('even', 0). No percent when the
    cost is zero (bonus units): a share of nothing is not a number."""
    if gain.paise == 0:
        return "even", (0 if cost.paise else None)
    word = "up" if gain.paise > 0 else "down"
    text = f"{word} {rupees(Money(paise=abs(gain.paise)))}"
    if cost.paise <= 0:
        return text, None
    t = tenths_of(abs(gain.paise), cost.paise)
    return f"{text} ({pct(t)})", t


def _unknown_cost_reason(book: Book, isins: set[Isin]) -> str:
    """Why cost is unknown, by where the units are held. A fund folio's cost
    comes from a statement that starts at the first purchase; a demat
    statement (NSDL/CDSL) lists balances only, so no statement gives its cost."""
    kinds: set[str] = set()
    for h in book.holdings():
        if h.isin in isins and h.cost is None:
            acct = book.account(h.account)
            kinds.add(acct.kind if acct else "demat")
    parts: list[str] = []
    if "mf_folio" in kinds:
        parts.append(
            "Your fund statements start after some units were bought, so their cost "
            "isn't known. A statement from your first investment fixes this."
        )
    if "demat" in kinds:
        parts.append(
            "Demat statements (NSDL, CDSL) list what you hold, not what you paid, so "
            "the cost of shares and ETFs there stays unknown until Stockin can read "
            "your broker's tradebook."
        )
    return " ".join(parts)


def cost_coverage(book: Book, prices: PriceBook) -> Insight | None:
    """Of the priced value, how much has a known cost, and the gain on it.
    Unknown cost (bought before your statements) and no price are told apart."""
    ws, total, unpriced = weights(book, prices)
    if not ws or total.paise <= 0:
        return None
    known = [w for w in ws if w.cost is not None]
    known_value = Money(paise=sum(w.value.paise for w in known))
    known_cost = Money(paise=sum(w.cost.paise for w in known if w.cost is not None))
    unknown_value = total.paise - known_value.paise
    gain = known_value - known_cost
    unknown_t = tenths_of(unknown_value, total.paise)
    words, gain_t = _gain_words(gain, known_cost)
    numbers: dict[str, int | str] = {
        "unknown_tenths": unknown_t,
        "unknown_value_paise": unknown_value,
        "priced_value_paise": total.paise,
        "known_value_paise": known_value.paise,
        "known_cost_paise": known_cost.paise,
        "gain_paise": gain.paise,
        "unpriced": len(unpriced),
    }
    if gain_t is not None:
        numbers["gain_tenths"] = gain_t
    of = "of what's priced" if unpriced else "of your money"
    if unknown_value > 0:
        headline = f"Gain is unknown for {pct(unknown_t)} {of}."
        detail = _unknown_cost_reason(book, {w.isin for w in ws if w.cost is None})
        if known:
            detail += f" On the rest you're {words}."
        tone: Tone = "unknown"
    else:
        what = "what's priced" if unpriced else "what you hold"
        headline = f"You're {words} on {what}."
        detail = "Value at the latest closing prices, minus what you paid (FIFO, with charges)."
        tone = "good" if gain.paise > 0 else "warn" if gain.paise < 0 else "info"
    detail += _unpriced_note(book, unpriced)
    return Insight(
        kind="cost_coverage",
        tone=tone,
        headline=headline,
        detail=detail,
        numbers=numbers,
        unit="paise",
        series=(
            Point(label="cost known", value=known_value.paise),
            Point(label="cost unknown", value=unknown_value),
        ),
    )


def _txns(book: Book) -> list[Txn]:
    return [f for f in book.facts if isinstance(f, Txn)]


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


def _fresh(t: Txn) -> bool:
    """New money in or out: not either half of a dividend reinvestment. The
    import labels those from the statement's own row type."""
    return not (isinstance(t.event, Buy | Income) and t.event.reinvested)


def _day(d: date) -> str:
    return f"{d.day} {_MON[d.month - 1]} {d.year}"


def what_changed(book: Book) -> Insight | None:
    """Money in and out since the previous statement. The market's share of
    the change needs prices on both dates, which Stockin does not keep yet,
    so this insight says only what you did."""
    covs = [f for f in book.facts if isinstance(f, Coverage)]
    txns = _txns(book)
    if not covs or not txns:
        return None
    ends = sorted({c.end for c in covs})
    end = ends[-1]
    start = ends[-2] + timedelta(days=1) if len(ends) > 1 else min(c.start for c in covs)
    window = [t for t in txns if start <= t.on <= end]
    added = sum(t.event.amount.paise for t in window if isinstance(t.event, Buy) and _fresh(t))
    out = sum(t.event.amount.paise for t in window if isinstance(t.event, Sell))
    income = sum(t.event.amount.paise for t in window if isinstance(t.event, Income) and _fresh(t))
    again = sum(
        t.event.amount.paise for t in window if isinstance(t.event, Income) and not _fresh(t)
    )
    buys = sum(1 for t in window if isinstance(t.event, Buy) and _fresh(t))
    sells = sum(1 for t in window if isinstance(t.event, Sell))
    headline = f"From {_day(start)} to {_day(end)}: you added {rupees(Money(paise=added))}" + (
        f" and took out {rupees(Money(paise=out))}." if out else "."
    )
    detail = _plural(buys, "purchase")
    if sells:
        detail += f", {_plural(sells, 'sale')}"
    if income:
        detail += f", {rupees(Money(paise=income))} paid out as dividends"
    if again:
        detail += f", {rupees(Money(paise=again))} of dividends reinvested"
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
            "reinvested_paise": again,
            "buys": buys,
            "sells": sells,
        },
        unit="paise",
        series=(
            Point(label="added", value=added),
            Point(label="taken out", value=-out),
        ),
    )


STOPPED_AFTER = 3
"""A SIP with this many missed months in a row up to the latest statement is
treated as stopped, not as missing instalments."""


def _sip_date(ym: tuple[int, int], day: int) -> date:
    """The SIP's day in month `ym`; the 31st is the month's last day."""
    import calendar

    return date(ym[0], ym[1], min(day, calendar.monthrange(*ym)[1]))


def sip_discipline(book: Book) -> Insight | None:
    """A SIP is a mutual fund in one account bought (new money, not a dividend
    reinvestment) in at least 3 different months. Its day is the day of the
    latest purchase.

    A month is due when that day falls inside a statement for the fund: a
    statement that ends before the SIP day, or a gap between statements,
    makes no month due. A sold-out fund is due up to its last purchase. 3 or
    more missed months in a row at the end means the SIP stopped: those are
    not counted as missed, and the stop is said instead."""
    spans: dict[tuple[AccountId, Isin], list[tuple[date, date]]] = defaultdict(list)
    for c in book.facts:
        if isinstance(c, Coverage):
            spans[(c.account, c.isin)].append((c.start, c.end))
    held = {(h.account, h.isin) for h in book.holdings()}
    buys: dict[tuple[AccountId, Isin], list[date]] = defaultdict(list)
    for t in _txns(book):
        if isinstance(t.event, Buy) and _fresh(t):
            inst = book.instrument(t.isin)
            if inst is not None and inst.kind == "mutual_fund":
                buys[(t.account, t.isin)].append(t.on)
    sips = {k: sorted(v) for k, v in buys.items() if len({_month(d) for d in v}) >= 3}
    if not sips:
        return None
    expected = paid = 0
    stopped: list[str] = []
    series: list[Point] = []
    for (acct, isin), days in sorted(sips.items()):
        inst = book.instrument(isin)
        name = short_name(inst.name) if inst else isin
        months = {_month(d) for d in days}
        sip_day = days[-1].day
        covered = spans.get((acct, isin), [])
        if (acct, isin) in held and covered:
            last = max(_month(days[-1]), max(_month(e) for _, e in covered))
        else:
            last = _month(days[-1])  # sold out: nothing is due after the last purchase

        due = [
            ym
            for ym in _months(min(months), last)
            if ym in months or any(s <= _sip_date(ym, sip_day) <= e for s, e in covered)
        ]
        trailing = 0
        for ym in reversed(due):
            if ym in months:
                break
            trailing += 1
        if trailing >= STOPPED_AFTER:
            stopped.append(name)
            due = due[: len(due) - trailing]
        for ym in due:
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
    across = f"across {_plural(n, 'SIP')}"
    headline = (
        f"All {expected} SIP instalments paid {across}."
        if not missed
        else f"{missed} of {expected} SIP instalments missed {across}."
    )
    detail = (
        "Paying every month means you buy more units when prices are low."
        if not missed
        else "A missed month is a month you didn't buy, often when prices were lower."
    )
    if stopped:
        detail += (
            f" {_names(sorted(stopped))} {'looks' if len(stopped) == 1 else 'look'} stopped: "
            f"no purchase in the last {STOPPED_AFTER} or more months."
        )
    return Insight(
        kind="sip_discipline",
        tone="good" if not missed else "warn",
        headline=headline,
        detail=detail,
        numbers={
            "expected": expected,
            "paid": paid,
            "missed": missed,
            "sips": n,
            "stopped": len(stopped),
        },
        unit="count",
        series=tuple(series),
    )


def long_term_from(acquired: date) -> date:
    """Equity is long-term when held more than 12 months: from the day after
    the first anniversary. A 29 Feb purchase's anniversary is 28 Feb, so it
    turns long-term on 1 Mar, like every other date's 366th-or-later day."""
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:
        anniversary = date(acquired.year + 1, 2, 28)
    return anniversary + timedelta(days=1)


def tax_clock(book: Book, today: date) -> Insight | None:
    """The next equity lots to turn long-term after `today`. The chart is the
    cost turning long-term on each upcoming date; the headline is its first bar."""
    upcoming: dict[date, list[tuple[str, Money]]] = defaultdict(list)
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
                upcoming[lt].append((short_name(inst.name), lot.cost))
    if not upcoming:
        return None
    when = min(upcoming)
    lots = upcoming[when]
    names = sorted({n for n, _ in lots})
    cost = Money(paise=sum(c.paise for _, c in lots))
    days = (when - today).days
    if cost.paise > 0:
        headline = f"{rupees(cost)} (at cost) of {_names(names)} turn long-term on {_day(when)}."
    else:  # bonus units: nothing was paid for them
        headline = f"Bonus units of {_names(names)} turn long-term on {_day(when)}."
    detail = (
        f"That's in {_plural(days, 'day')}. Selling before then is taxed as short-term gains. "
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
            Point(label=_day(d), value=sum(c.paise for _, c in v), key=d.isoformat())
            for d, v in sorted(upcoming.items())[:12]
        ),
    )


def _fy(d: date) -> int:
    """FY start year: 15 Jan 2026 is FY 2025-26 -> 2025."""
    return d.year if d.month >= 4 else d.year - 1


def invested(book: Book) -> Insight | None:
    """New money in minus money out, by financial year. A reinvested
    dividend is neither."""
    net: dict[int, int] = defaultdict(int)
    for t in _txns(book):
        if isinstance(t.event, Buy) and _fresh(t):
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
