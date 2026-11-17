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
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import cas_fixtures as fx
import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from stockin.book import Book, reconcile
from stockin.facts import Buy, Isin, Sell, Txn
from stockin.money import Money, Price, Qty, div_half_even
from stockin.prices import PriceBook, PriceStore, Quote
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
            trades.append(Trade(month, day, not sell, units, amount, tax))
        out[isin] = trades
    return out


def _units(trades: list[Trade], upto_month: int) -> Decimal:
    return sum(
        (t.units if t.buy else -t.units for t in trades if t.month <= upto_month), Decimal(0)
    )


def _rows(t: Trade) -> list[dict[str, Any]]:
    on = f"2026-{t.month:02d}-{t.day:02d}"
    if t.buy:
        rows = [fx.txn(on, "PURCHASE", str(t.units), str(t.amount))]
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
    parsed = {q.isin: q for q in eod.parse_amfi("\n".join(lines))}
    assert parsed == {
        Isin(i): Quote(isin=Isin(i), on=on, price=Price.rupees(p), source="amfi")
        for i, on, p in rows
    }


@given(quotes(), st.randoms(use_true_random=False))
def test_best_price_does_not_depend_on_order(qs: list[Quote], rnd: random.Random) -> None:
    shuffled = qs[:]
    rnd.shuffle(shuffled)
    assert PriceBook.best(qs) == PriceBook.best(shuffled + qs[:3])


@given(quotes(), st.randoms(use_true_random=False), quote_days)
def test_price_store_is_order_and_repeat_free_and_reads_like_best(
    qs: list[Quote], rnd: random.Random, cutoff: date
) -> None:
    shuffled = qs + qs[: len(qs) // 2]
    rnd.shuffle(shuffled)
    with tempfile.TemporaryDirectory() as d, PriceStore.open(Path(d) / "a.db", write=True) as a:
        a.put(qs)
        with PriceStore.open(Path(d) / "b.db", write=True) as b:
            b.put(shuffled)
            assert a.digest() == b.digest()
            # what the store reads equals the pure rule over the stored rows
            stored = PriceBook.best(q for q in qs if q.on <= cutoff)
            kept: dict[tuple[str, date, str], Quote] = {}
            for q in qs:
                k = (q.isin, q.on, q.source)
                if k not in kept or q.price.micro_rupees > kept[k].price.micro_rupees:
                    kept[k] = q
            expected = PriceBook.best(q for q in kept.values() if q.on <= cutoff)
            assert stored.quotes.keys() == expected.quotes.keys()
            assert b.book(ISINS, on_or_before=cutoff) == expected  # type: ignore[arg-type]
