"""Property-based tests: invariants that must hold for every input, not just
the hand-picked fixtures. Hypothesis generates the inputs and shrinks any
failure to the smallest one that breaks the rule.

The statement generator builds one true trading history per scheme, then cuts
CAS statements out of it as month windows, the way CAMS issues them for any
period you ask. Overlapping, gapped and repeated windows are all fair game."""

from __future__ import annotations

import calendar
import hashlib
import random
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, getcontext
from fractions import Fraction
from pathlib import Path
from typing import Any

import cas_fixtures as fx
import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from stockin.book import Book, reconcile
from stockin.facts import Buy, Isin, Sell, Txn
from stockin.money import Bps, Money, Price, Qty, div_half_even
from stockin.networth import (
    BUCKETS,
    KIND_BUCKET,
    AssetStore,
    Closed,
    Entry,
    Fd,
    Gold,
    Stated,
    _add_months,  # pyright: ignore[reportPrivateUsage]
    current,
    entry_value,
    fd_maturity,
    fd_value,
    label_key,
    networth,
    shares,
)
from stockin.prices import PriceBook, PriceFile, PriceStore, Quote, is_jump, left_out
from stockin.sources import cas, eod
from stockin.store import Ledger, LedgerWriter

# ------------------------------------------------------------------- money

paise = st.integers(min_value=-(10**15), max_value=10**15)
micros = st.integers(min_value=0, max_value=10**15)


@given(paise)
def test_money_prints_and_parses_back_exactly(p: int) -> None:
    m = Money(paise=p)
    assert Money.rupees(str(m)) == m


@given(micros)
def test_qty_prints_and_parses_back_exactly(n: int) -> None:
    q = Qty(micros=n)
    assert Qty.parse(str(q)) == q


@given(st.integers(-(10**18), 10**18), st.integers(1, 10**12))
def test_half_even_division_matches_exact_rounding(num: int, den: int) -> None:
    assert div_half_even(num, den) == round(Fraction(num, den))  # round() is half-even


@given(st.integers(-(10**12), 10**12), st.integers(1, 10**6))
def test_exact_halves_round_to_even(q: int, d: int) -> None:
    """Random draws almost never land on an exact .5, so build ties directly."""
    num, den = q * 2 * d + d, 2 * d  # num / den == q + 1/2
    assert div_half_even(num, den) == round(Fraction(num, den))


@given(paise, st.integers(1, 10**12), st.data())
def test_a_split_share_is_the_half_even_share_and_never_leaks(
    p: int, whole: int, data: st.DataObject
) -> None:
    part = data.draw(st.integers(0, whole) | st.just(whole // 2))
    share, rest = Money(paise=p).split(Qty(micros=part), Qty(micros=whole))
    assert share.paise == round(Fraction(p * part, whole))
    assert share + rest == Money(paise=p)


# --------------------------------------------------------- statement model


@dataclass(frozen=True)
class Trade:
    month: int  # 1..6 of 2026
    day: int
    buy: bool
    units: Decimal  # 3 decimals, positive
    amount: Decimal  # 2 decimals, positive
    tax: Decimal | None  # stamp duty on a buy, STT on a sell
    reinvest: bool = False  # a buy that is an IDCW reinvested, not new money


SCHEMES = {fx.HDFC_MF: "HDFC Flexi Cap Fund - Direct Growth", fx.ICICI_MF: "ICICI Bluechip"}
MONTHS = 6


@st.composite
def histories(draw: st.DrawFn, one_trade_a_day: bool = False) -> dict[str, list[Trade]]:
    """A true, valid history per scheme: units never go below zero. With
    `one_trade_a_day`, no two trades of a scheme share a date, so FIFO order
    is the statement order (same-day lots are ordered by fact id instead)."""
    out: dict[str, list[Trade]] = {}
    for isin in draw(st.sampled_from([[fx.HDFC_MF], [fx.ICICI_MF], list(SCHEMES)])):
        held = Decimal(0)
        trades: list[Trade] = []
        raw = draw(
            st.lists(
                st.tuples(st.integers(1, MONTHS), st.integers(1, 28)),
                min_size=1,
                max_size=12,
                unique=one_trade_a_day,
            )
        )
        for month, day in sorted(raw):
            sell = held > 0 and draw(st.booleans())
            if sell:
                units = Decimal(draw(st.integers(1, int(held * 1000)))) / 1000
                held -= units
            else:
                units = Decimal(draw(st.integers(1, 5_000_000))) / 1000
                held += units
            amount = Decimal(draw(st.integers(100, 10**9))) / 100  # rupees, >= 1.00
            tax = draw(st.none() | st.integers(1, 99).map(lambda c: Decimal(c) / 100))
            again = not sell and draw(st.booleans())
            trades.append(Trade(month, day, not sell, units, amount, tax, again))
        out[isin] = trades
    return out


def _units(trades: list[Trade], upto_month: int) -> Decimal:
    return sum(
        (t.units if t.buy else -t.units for t in trades if t.month <= upto_month), Decimal(0)
    )


def _rows(t: Trade) -> list[dict[str, Any]]:
    on = f"2026-{t.month:02d}-{t.day:02d}"
    if t.buy:
        kind = "DIVIDEND_REINVEST" if t.reinvest else "PURCHASE"
        rows = [fx.txn(on, kind, str(t.units), str(t.amount))]
        if t.tax is not None:
            rows.append(fx.txn(on, "STAMP_DUTY_TAX", None, str(t.tax)))
    else:
        rows = [fx.txn(on, "REDEMPTION", str(-t.units), str(t.amount))]
        if t.tax is not None:
            rows.append(fx.txn(on, "STT_TAX", None, str(t.tax)))
    return rows


def _statement(
    history: dict[str, list[Trade]], first: int, last: int, *, dated_names: bool = False
) -> fx.CASData:
    """The CAS that CAMS would issue for months first..last. With
    `dated_names`, each statement names the scheme after its last month, the
    way a fund renamed mid-year shows its new name only in later statements."""
    schemes = [
        fx.scheme(
            isin,
            f"{SCHEMES[isin]} (as of {last})" if dated_names else SCHEMES[isin],
            str(_units(trades, first - 1)),
            str(_units(trades, last)),
            [r for t in trades if first <= t.month <= last for r in _rows(t)],
        )
        for isin, trades in history.items()
    ]
    end_day = calendar.monthrange(2026, last)[1]
    return fx.rta(
        f"01-{calendar.month_abbr[first]}-2026",
        f"{end_day:02d}-{calendar.month_abbr[last]}-2026",
        [fx.folio("9000001/01", "Test Mutual Fund", schemes)],
    )


def _sha(data: fx.CASData) -> str:
    return hashlib.sha256(data.model_dump_json(by_alias=True).encode()).hexdigest()


windows = st.lists(
    st.tuples(st.integers(1, MONTHS), st.integers(1, MONTHS)).map(lambda w: (min(w), max(w))),
    min_size=1,
    max_size=4,
)

# ----------------------------------------------------------- import mapping


@given(histories(), st.tuples(st.integers(1, MONTHS), st.integers(1, MONTHS)))
def test_a_valid_statement_always_imports_and_balances(
    history: dict[str, list[Trade]], w: tuple[int, int]
) -> None:
    first, last = min(w), max(w)
    stmt = cas.from_casparser(_statement(history, first, last), "sha")
    for isin, trades in history.items():
        moved = sum(
            (
                f.event.units.micros if isinstance(f.event, Buy) else -f.event.units.micros
                for f in stmt.facts
                if isinstance(f, Txn) and f.isin == isin and isinstance(f.event, Buy | Sell)
            ),
            0,
        )
        expected = _units(trades, last) - _units(trades, first - 1)
        assert moved == int(expected * 1_000_000)


@given(histories(), st.integers(1, 999))
def test_a_statement_whose_closing_does_not_add_up_is_rejected(
    history: dict[str, list[Trade]], off: int
) -> None:
    data = _statement(history, 1, MONTHS).model_dump(by_alias=True)
    scheme = data["folios"][0]["schemes"][0]
    scheme["close"] = str(Decimal(scheme["close"]) + Decimal(off) / 1000)
    with pytest.raises(cas.CasRejected, match="!= closing"):
        cas.from_casparser(fx.CASData.model_validate(data), "sha")


# ---------------------------------------------------------- ledger + book


def _ingest_all(db: Path, statements: list[fx.CASData]) -> str:
    with LedgerWriter.open(db) as w:
        for s in statements:
            w.ingest(cas.from_casparser(s, _sha(s)))
        return w.digest()


@given(histories(), windows, st.randoms(use_true_random=False))
def test_ledger_is_the_same_for_any_order_and_any_repeats(
    history: dict[str, list[Trade]], ws: list[tuple[int, int]], rnd: random.Random
) -> None:
    statements = [_statement(history, a, b) for a, b in ws]
    shuffled = statements + rnd.sample(statements, k=rnd.randint(0, len(statements)))
    rnd.shuffle(shuffled)
    with tempfile.TemporaryDirectory() as tmp:
        assert _ingest_all(Path(tmp) / "a.db", statements) == _ingest_all(
            Path(tmp) / "b.db", shuffled
        )


@given(histories(), windows)
def test_statements_cut_from_one_true_history_never_mismatch(
    history: dict[str, list[Trade]], ws: list[tuple[int, int]]
) -> None:
    """Any mix of overlapping or gapped statements of the same truth: reconcile
    finds no mismatch, and holdings equal the latest statement's closing."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "l.db"
        _ingest_all(db, [_statement(history, a, b) for a, b in ws])
        with Ledger.open(db) as led:
            book = led.book()
    report = reconcile(book)
    assert report.ok, [c for c in report.mismatches]
    latest = max(b for _, b in ws)
    held: dict[str, Qty] = {h.isin: h.qty for h in book.holdings()}
    for isin, trades in history.items():
        closing = _units(trades, latest)
        if closing:
            assert held.get(isin) == Qty.parse(str(closing))
        else:
            assert isin not in held


@given(histories(), windows, st.data())
def test_retract_is_the_exact_inverse_of_ingest(
    history: dict[str, list[Trade]], ws: list[tuple[int, int]], data: st.DataObject
) -> None:
    statements = [_statement(history, a, b) for a, b in ws]
    extra = _statement(history, *data.draw(windows)[0])
    assume(_sha(extra) not in {_sha(s) for s in statements})  # else it is not "extra"
    with tempfile.TemporaryDirectory() as tmp, LedgerWriter.open(Path(tmp) / "l.db") as w:
        for s in statements:
            w.ingest(cas.from_casparser(s, _sha(s)))
        before = w.digest()
        w.ingest(cas.from_casparser(extra, _sha(extra)))
        w.retract(_sha(extra))
        assert w.digest() == before


@given(histories(), windows, st.randoms(use_true_random=False))
def test_holdings_do_not_depend_on_fact_order(
    history: dict[str, list[Trade]], ws: list[tuple[int, int]], rnd: random.Random
) -> None:
    facts = [
        f for a, b in ws for f in cas.from_casparser(_statement(history, a, b), f"{a}-{b}").facts
    ]
    shuffled = facts[:]
    rnd.shuffle(shuffled)
    one, two = Book.of(tuple(facts)), Book.of(tuple(shuffled))
    assert one.holdings() == two.holdings()
    assert reconcile(one) == reconcile(two)
    latest = max(b for _, b in ws)
    expected = {
        isin: Qty.parse(str(_units(trades, latest)))
        for isin, trades in history.items()
        if _units(trades, latest)
    }
    assert {h.isin: h.qty for h in one.holdings()} == expected  # right, not just consistent


def _fifo(trades: list[Trade], upto: int) -> tuple[int, list[tuple[int, int, int]]]:
    """An independent model of cost: (cost paise of what is held, and one
    (micros, cost paise, proceeds paise) per lot each sell touches). FIFO lots;
    stamp duty adds to a buy's cost, STT comes off a sell's proceeds; a
    part-lot's cost, and each lot's part of the proceeds, is its half-even share."""
    lots: list[list[int]] = []  # [micros, cost paise]
    disposals: list[tuple[int, int, int]] = []
    for t in trades:
        if t.month > upto:
            continue
        micros, paise = int(t.units * 1_000_000), int(t.amount * 100)
        tax = int(t.tax * 100) if t.tax is not None else 0
        if t.buy:
            lots.append([micros, paise + tax])
            continue
        proceeds = paise - tax
        while micros:
            lot = lots[0]
            take = min(lot[0], micros)
            cost = round(Fraction(lot[1] * take, lot[0]))
            part = round(Fraction(proceeds * take, micros))
            disposals.append((take, cost, part))
            lot[0], lot[1] = lot[0] - take, lot[1] - cost
            micros, proceeds = micros - take, proceeds - part
            if not lot[0]:
                lots.pop(0)
    return sum(c for _, c in lots), disposals


@given(histories(one_trade_a_day=True), st.integers(1, MONTHS))
def test_cost_and_proceeds_match_an_independent_fifo_model(
    history: dict[str, list[Trade]], last: int
) -> None:
    """Money, not just units: a statement from the first month on gives every
    lot, so held cost and every disposal's cost and proceeds are fully known
    and must match the model lot by lot (each disposal's gain depends on it)."""
    book = Book.of(cas.from_casparser(_statement(history, 1, last), "sha").facts)
    held = {h.isin: h for h in book.holdings()}
    for acct, isin in book.pairs():
        cost, disposals = _fifo(history[isin], last)
        got = [
            (
                d.qty.micros,
                d.cost.paise if d.cost else None,
                d.proceeds.paise if d.proceeds else None,
            )
            for d in book.timeline(acct, isin).disposals
        ]
        assert got == disposals
        if isin in held:
            assert held[isin].cost == Money(paise=cost)


@given(histories(), st.integers(1, MONTHS))
def test_money_is_conserved_whatever_the_same_day_order(
    history: dict[str, list[Trade]], last: int
) -> None:
    """Holds even with several trades on one day, where lot order is by fact
    id: every rupee paid in is either still held or sold, and every rupee of
    net sale proceeds lands in exactly one disposal."""
    book = Book.of(cas.from_casparser(_statement(history, 1, last), "sha").facts)
    held = {h.isin: h for h in book.holdings()}
    for acct, isin in book.pairs():
        trades = [t for t in history[isin] if t.month <= last]
        tax = {id(t): int(t.tax * 100) if t.tax is not None else 0 for t in trades}
        paid = sum(int(t.amount * 100) + tax[id(t)] for t in trades if t.buy)
        received = sum(int(t.amount * 100) - tax[id(t)] for t in trades if not t.buy)
        disposals = book.timeline(acct, isin).disposals
        h = held.get(isin)
        held_cost = h.cost.paise if h is not None and h.cost is not None else 0
        assert held_cost + sum(d.cost.paise for d in disposals if d.cost) == paid
        assert sum(d.proceeds.paise for d in disposals if d.proceeds) == received


@given(histories(), windows, st.randoms(use_true_random=False))
def test_instrument_details_come_from_the_latest_statement_in_any_order(
    history: dict[str, list[Trade]], ws: list[tuple[int, int]], rnd: random.Random
) -> None:
    statements = [_statement(history, a, b, dated_names=True) for a, b in ws]
    rnd.shuffle(statements)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "l.db"
        _ingest_all(db, statements)
        with Ledger.open(db) as led:
            book = led.book()
    latest = max(b for _, b in ws)
    for isin in history:
        inst = book.instrument(isin)  # type: ignore[arg-type]
        assert inst is not None and inst.name == f"{SCHEMES[isin]} (as of {latest})"


# ------------------------------------------------------------- demat CAS

STOCKS = (fx.INFY, fx.TCS, fx.HDFCBANK)


@st.composite
def demat_months(draw: st.DrawFn) -> list[dict[str, int]]:
    """Shares held at each month end; 0 means sold out (absent from the CAS)."""
    return [
        {isin: draw(st.sampled_from([0, 0, 1, 5, 12, 100])) for isin in STOCKS}
        for _ in range(MONTHS)
    ]


def _demat(held: list[dict[str, int]], first: int, last: int) -> fx.NSDLCASData:
    end_day = calendar.monthrange(2026, last)[1]
    return fx.demat(
        f"01-{calendar.month_abbr[first]}-2026",
        f"{end_day:02d}-{calendar.month_abbr[last]}-2026",
        {isin: str(n) for isin, n in held[last - 1].items() if n},
    )


@given(demat_months(), windows, st.randoms(use_true_random=False))
def test_demat_holdings_follow_the_latest_statement_in_any_order(
    held: list[dict[str, int]], ws: list[tuple[int, int]], rnd: random.Random
) -> None:
    """Includes stocks that are sold out, bought back and sold again: a stock
    missing from the latest statement must not linger as a phantom holding."""
    statements = [_demat(held, a, b) for a, b in ws]
    rnd.shuffle(statements)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "l.db"
        with LedgerWriter.open(db) as w:
            for s in statements:
                w.ingest(
                    cas.from_casparser(s, hashlib.sha256(s.model_dump_json().encode()).hexdigest())
                )
        with Ledger.open(db) as led:
            book = led.book()
    assert reconcile(book).ok
    latest = held[max(b for _, b in ws) - 1]
    assert {h.isin: str(h.qty) for h in book.holdings()} == {
        isin: str(n) for isin, n in latest.items() if n
    }


# ------------------------------------------------------------------ prices


def _with_check_digit(body: str) -> str:
    from stockin.facts import parse_isin

    for d in "0123456789":
        try:
            return parse_isin(body + d)
        except ValueError:
            continue
    raise AssertionError("no check digit")  # pragma: no cover


ISINS = [_with_check_digit(f"INF{n:08d}") for n in range(6)]
price_text = st.builds(
    lambda whole, frac: f"{whole}.{frac:04d}", st.integers(1, 99_999), st.integers(0, 9_999)
)
quote_days = st.dates(min_value=date(2026, 1, 1), max_value=date(2026, 1, 31))


@st.composite
def quotes(draw: st.DrawFn) -> list[Quote]:
    rows = draw(
        st.lists(
            st.tuples(
                st.sampled_from(ISINS),
                quote_days,
                price_text,
                st.sampled_from(["amfi", "bhavcopy"]),
            ),
            max_size=25,
        )
    )
    return [
        Quote(isin=Isin(i), on=on, price=Price.rupees(p), source=src)  # type: ignore[arg-type]
        for i, on, p, src in rows
    ]


@given(
    st.lists(
        st.tuples(st.sampled_from(ISINS), quote_days, price_text),
        min_size=1,  # a file with no NAV rows is rejected: see test_prices
        max_size=20,
        unique_by=lambda t: t[0],
    )
)
def test_amfi_file_round_trips(rows: list[tuple[str, date, str]]) -> None:
    lines = [
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
        "Scheme Name;Plan;Option;Net Asset Value;Date",
        " ",
        "Some AMC",
        " ",
    ]
    for n, (isin, on, p) in enumerate(rows):
        lines.append(f"{n};{isin};-;Fund {n};Direct Plan;Growth;{p};{on.strftime('%d-%b-%Y')}")
    days = [on for _, on, _ in rows]
    file = eod.parse_amfi("\n".join(lines), max(days))  # the newest row is dated today
    assert {q.isin: q for q in file.quotes} == {
        Isin(i): Quote(isin=Isin(i), on=on, price=Price.rupees(p), source="amfi")
        for i, on, p in rows
    }
    assert file.day == max(days, key=lambda d: (days.count(d), d))  # the date most rows carry
    with pytest.raises(eod.EodRejected):
        eod.parse_amfi("\n".join(lines), max(days) - timedelta(days=1))


@given(quotes(), st.randoms(use_true_random=False))
def test_best_price_does_not_depend_on_order(qs: list[Quote], rnd: random.Random) -> None:
    shuffled = qs[:]
    rnd.shuffle(shuffled)
    assert PriceBook.best(qs) == PriceBook.best(shuffled + qs[:3])


@st.composite
def price_files(draw: st.DrawFn, source: str | None = None, day: date | None = None) -> PriceFile:
    """A file as a parser would hand it over: one quote per ISIN. Prices span
    1 to 99,999 rupees, so jumps between files are common."""
    src = source or draw(st.sampled_from(["amfi", "bhavcopy"]))
    on = day or draw(quote_days)
    rows = draw(
        st.lists(st.tuples(st.sampled_from(ISINS), price_text, st.integers(0, 3)), max_size=6)
    )
    found: dict[str, Quote] = {}
    for isin, price, age in rows:
        dated = on if src == "bhavcopy" else on - timedelta(days=age)  # a NAV can be days old
        found[isin] = Quote(isin=Isin(isin), on=dated, price=Price.rupees(price), source=src)  # type: ignore[arg-type]
    return PriceFile(source=src, day=on, quotes=tuple(found.values()))  # type: ignore[arg-type]


def _model_book(files: list[PriceFile], cutoff: date) -> PriceBook:
    """The rule in plain Python: the last file per (source, day) is that
    day's word; per ISIN each source's newest day speaks; then best()."""
    last = {(f.source, f.day): f for f in files}
    newest: dict[tuple[str, str], tuple[date, Quote]] = {}
    for (source, day), f in last.items():
        for q in f.quotes:
            key = (q.isin, source)
            if day <= cutoff and (key not in newest or day > newest[key][0]):
                newest[key] = (day, q)
    return PriceBook.best(q for _, q in newest.values())


@given(st.lists(price_files(), max_size=12), st.randoms(use_true_random=False), quote_days)
def test_price_store_reads_like_the_last_file_of_each_day_and_is_repeat_free(
    files: list[PriceFile], rnd: random.Random, cutoff: date
) -> None:
    with tempfile.TemporaryDirectory() as d, PriceStore.open(Path(d) / "a.db", write=True) as a:
        for f in files:
            a.replace(f)
        assert a.book(ISINS, on_or_before=cutoff) == _model_book(files, cutoff)  # type: ignore[arg-type]
        assert a.book(ISINS) == _model_book(files, date.max)  # type: ignore[arg-type]
        # the last file of each day, alone, in any order and any number of times
        final = list({(f.source, f.day): f for f in files}.values())
        again = final + final[: len(final) // 2]
        rnd.shuffle(again)
        with PriceStore.open(Path(d) / "b.db", write=True) as b:
            for f in again:
                b.replace(f)
            assert a.digest() == b.digest()
            assert all(b.replace(f) == 0 for f in final)


@given(st.data(), st.lists(price_files(), max_size=6), st.sampled_from(["amfi", "bhavcopy"]))
def test_price_store_is_restored_by_the_right_file_after_any_damaged_copies(
    data: st.DataObject, others: list[PriceFile], source: str
) -> None:
    """The bar for a file without a checksum (lesson 18): whatever damaged
    copies of a day's file were imported, importing the right one leaves
    prices.db exactly as if they never were, jump check included. Nothing is
    ever stored that jumped from an earlier day without being accepted."""
    day = date(2026, 1, 15)
    right = data.draw(price_files(source=source, day=day))
    damaged = data.draw(st.lists(price_files(source=source, day=day), min_size=1, max_size=3))
    others = [f for f in others if (f.source, f.day) != (source, day)]
    with (
        tempfile.TemporaryDirectory() as d,
        PriceStore.open(Path(d) / "a.db", write=True) as a,
        PriceStore.open(Path(d) / "b.db", write=True) as b,
    ):
        for f in others:
            a.replace(f)
            b.replace(f)
        for f in damaged:
            b.take(f)
        assert a.take(right) == (len(a.stored(source, day)), b.take(right)[1])  # type: ignore[arg-type]
        assert a.digest() == b.digest()
        assert a.take(right) == (0, a.take(right)[1])  # and again changes nothing
        before = _model_book(others, day - timedelta(days=1))
        stored = a.stored(source, day)  # type: ignore[arg-type]
        for q in right.quotes:
            last = before.get(q.isin)
            held = last is not None and is_jump(last.price, q.price)
            assert (q.isin in stored) == (not held), q
        # accepting them stores the whole file, as it is
        a.take(right, accept_jumps=True)
        assert a.stored(source, day) == {q.isin: q for q in right.quotes}  # type: ignore[arg-type]
        assert a.take(right) == (0, ())  # accepted once, not held again


@given(
    st.data(),
    st.lists(price_files(), max_size=4),
    st.sampled_from(["amfi", "bhavcopy"]),
    st.frozensets(st.sampled_from(ISINS)),
    st.booleans(),
)
def test_no_held_price_leaves_a_day_unreported_and_the_earlier_file_puts_it_back(
    data: st.DataObject, others: list[PriceFile], source: str, held: frozenset[str], accept: bool
) -> None:
    """Lesson 21: importing a file is always `replace`, so a second copy of
    a day can take a held price away (a file cut short, or the right file
    after a damaged one). Every such price is named, by `left_out` or as a
    held jump, with or without --accept-jumps; nothing not held is named;
    and importing the first copy again leaves prices.db as it was."""
    day = date(2026, 1, 15)
    first = data.draw(price_files(source=source, day=day))
    second = data.draw(price_files(source=source, day=day))
    others = [f for f in others if (f.source, f.day) != (source, day)]
    with tempfile.TemporaryDirectory() as d, PriceStore.open(Path(d) / "a.db", write=True) as a:
        for f in others:
            a.replace(f)
        a.take(first, accept_jumps=True)
        want = a.digest()
        before = a.stored(source, day)  # type: ignore[arg-type]
        gone = left_out(second, before, held)  # type: ignore[arg-type]
        _, jumps = a.take(second, accept_jumps=accept)
        after = a.stored(source, day)  # type: ignore[arg-type]
        named = {q.isin for q in gone} | {j.quote.isin for j in jumps}
        assert {i for i in before if i in held and i not in after} <= named
        assert {q.isin: q for q in gone} == {
            i: q
            for i, q in before.items()
            if i in held and i not in {q.isin for q in second.quotes}
        }
        assert all(q.isin not in after for q in gone)
        a.take(first, accept_jumps=True)
        assert a.digest() == want


# ---------------------------------------------------------------- net worth
# Sprint 2: the entries you type (bank, FD, EPF, gold, loans), the store that
# keeps them, and the picture built from them.


_NW_DAY0 = date(2024, 1, 1)
nw_days = st.integers(0, 1_400).map(lambda n: _NW_DAY0 + timedelta(days=n))
nw_labels = st.sampled_from(["SBI", "sbi", "HDFC  FD", "hdfc fd", "Home", "Gold", "PPF"])
nw_paise = st.integers(0, 10**12)


@st.composite
def fds(draw: st.DrawFn) -> Fd:
    return Fd(
        label=draw(nw_labels),
        as_of=draw(nw_days),
        principal=Money(paise=draw(st.integers(1, 10**12))),
        rate=Bps(draw(st.integers(0, 5_000))),
        start=draw(nw_days),
        months=draw(st.integers(1, 120)),
    )


def entries() -> st.SearchStrategy[Entry]:
    stated = st.builds(
        Stated,
        kind=st.sampled_from(["bank", "epf", "ppf", "nps", "property", "other", "loan"]),
        label=nw_labels,
        as_of=nw_days,
        amount=nw_paise.map(lambda p: Money(paise=p)),
    )
    gold = st.builds(
        Gold,
        label=nw_labels,
        as_of=nw_days,
        grams=st.integers(0, 10**9).map(lambda m: Qty(micros=m)),
        rate=st.integers(1, 10**10).map(lambda m: Price(micro_rupees=m)),
    )
    closed = st.builds(
        Closed,
        kind=st.sampled_from(["bank", "fd", "gold", "loan", "epf"]),
        label=nw_labels,
        as_of=nw_days,
    )
    return st.one_of(stated, fds(), gold, closed)


def _unique_days(es: list[Entry]) -> list[Entry]:
    """One entry per (kind, label, day): the store keeps one, by design."""
    seen: dict[tuple[str, str, date], Entry] = {}
    for e in es:
        seen.setdefault((e.kind, label_key(e.label), e.as_of), e)
    return list(seen.values())


@given(fds(), nw_days, nw_days)
def test_an_fd_never_shrinks_starts_at_its_principal_and_stops_at_maturity(
    fd: Fd, a: date, b: date
) -> None:
    early, late = min(a, b), max(a, b)
    assert fd.principal.paise <= fd_value(fd, early).paise <= fd_value(fd, late).paise
    assert fd_value(fd, fd.start) == fd.principal
    end = fd_maturity(fd)
    assert fd_value(fd, late) == fd_value(fd, min(late, end))


@given(fds(), st.integers(0, 40))
def test_on_each_quarter_day_an_fd_equals_the_compound_interest_formula(fd: Fd, q: int) -> None:
    """Oracle: P x (1 + r/4)^q, worked in Decimal at 80 digits, half-even."""
    assume(fd.months >= 6)
    day = _add_months(fd.start, 3 * q)
    assume(day <= fd_maturity(fd))
    getcontext().prec = 80
    want = Decimal(fd.principal.paise) * (1 + Decimal(fd.rate) / 40_000) ** q
    assert fd_value(fd, day).paise == int(want.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


@given(st.lists(st.integers(0, 10**15), min_size=1, max_size=8))
def test_shares_add_to_100_percent_and_each_is_within_a_basis_point(vs: list[int]) -> None:
    assume(sum(vs) > 0)
    out = shares(vs)
    assert sum(out) == 10_000
    for v, s in zip(vs, out, strict=True):
        assert abs(Fraction(v * 10_000, sum(vs)) - s) < 1


@given(st.lists(entries(), max_size=12), st.randoms(use_true_random=False), nw_days)
def test_the_current_entries_do_not_depend_on_order(
    es: list[Entry], rnd: random.Random, today: date
) -> None:
    es = _unique_days(es)
    shuffled = es[:]
    rnd.shuffle(shuffled)
    assert current(es, today) == current(shuffled, today)
    for e in current(es, today):
        later = [
            x
            for x in es
            if (x.kind, label_key(x.label)) == (e.kind, label_key(e.label)) and e.as_of < x.as_of
        ]
        assert all(x.as_of > today for x in later)  # nothing newer is ignored


@given(st.lists(entries(), max_size=10), st.randoms(use_true_random=False))
def test_the_store_round_trips_and_ignores_order_and_repeats(
    es: list[Entry], rnd: random.Random
) -> None:
    es = _unique_days(es)
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d) / "a.db", Path(d) / "b.db"
        with AssetStore.open(a) as store:
            for e in es:
                store.put(e)
        twice = es + es
        rnd.shuffle(twice)
        with AssetStore.open(b) as store:
            for e in twice:
                store.put(e)
        with AssetStore.open(a) as sa, AssetStore.open(b) as sb:
            assert sa.entries() == sb.entries()
            assert sorted(sa.entries(), key=repr) == sorted(es, key=repr)


targets_ = st.lists(st.integers(0, 10_000), min_size=len(BUCKETS), max_size=len(BUCKETS)).map(
    lambda xs: dict(zip(BUCKETS, shares(xs), strict=True)) if sum(xs) else {}
)


@given(st.lists(entries(), max_size=12), targets_, nw_days)
def test_net_worth_adds_up_and_the_mix_is_exactly_100_percent(
    es: list[Entry], targets: dict[str, int], today: date
) -> None:
    es = _unique_days(es)
    t = {b: Bps(v) for b, v in targets.items() if v > 0}
    nw = networth(None, PriceBook(quotes={}), es, t, today)  # type: ignore[arg-type]
    held = current(es, today)
    owned = sum(entry_value(e, today).paise for e in held if KIND_BUCKET[e.kind] is not None)
    owed = sum(entry_value(e, today).paise for e in held if KIND_BUCKET[e.kind] is None)
    assert (nw.owned.paise, nw.owed.paise) == (owned, owed)
    assert nw.total.paise == owned - owed
    assert sum(ln.value.paise for ln in nw.lines if ln.bucket) == owned
    if owned > 0:
        assert sum(s.bps for s in nw.slices) == 10_000
        for s in nw.slices:
            assert abs(Fraction(s.value.paise * 10_000, owned) - s.bps) < 1
            if s.target is not None:
                assert s.drift == s.bps - s.target
        if t:  # drifts of targeted classes balance the untargeted ones
            assert sum(s.drift or 0 for s in nw.slices) == -sum(
                s.bps for s in nw.slices if s.target is None
            )
    else:
        assert all(s.bps == 0 and s.drift is None for s in nw.slices)
