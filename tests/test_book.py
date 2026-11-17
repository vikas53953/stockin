"""Book.timeline against hand-worked synthetic fixtures. No real data."""

from datetime import date, timedelta
from itertools import permutations

import pytest

from stockin.book import Book, Inferred, reconcile
from stockin.facts import (
    Balance,
    Bonus,
    Buy,
    Complete,
    Coverage,
    Fact,
    FixedDeposit,
    GoldHolding,
    Instrument,
    Sell,
    Split,
    StatedBalance,
    Thesis,
    Txn,
    account_id,
    parse_isin,
)
from stockin.money import Money, Price, Qty, percent

FOLIO = account_id("mf_folio", "Example AMC", "1001")
DEMAT = account_id("demat", "IN300000", "10000001")
FUND = parse_isin("INF209K01YN0")  # public ISIN shape; units are invented
INFY = parse_isin("INE009A01021")


def q(text: str) -> Qty:
    return Qty.parse(text)


def rs(text: str) -> Money:
    return Money.rupees(text)


def buy(on: date, units: str, amount: str, nth: int = 0, acct=FOLIO, isin=FUND) -> Txn:
    return Txn(
        account=acct, isin=isin, on=on, event=Buy(units=q(units), amount=rs(amount)), nth=nth
    )


def sell(on: date, units: str, amount: str, acct=FOLIO, isin=FUND) -> Txn:
    return Txn(account=acct, isin=isin, on=on, event=Sell(units=q(units), amount=rs(amount)))


def bal(on: date, units: str, acct=FOLIO, isin=FUND) -> Balance:
    return Balance(account=acct, isin=isin, on=on, qty=q(units))


def cov(start: date, end: date, acct=FOLIO, isin=FUND) -> Coverage:
    return Coverage(account=acct, isin=isin, start=start, end=end)


def mf_statement(
    start: date, end: date, opening: str, closing: str, *txns: Txn
) -> tuple[Fact, ...]:
    """What a CAMS detailed CAS becomes: open balance, coverage, txns, close."""

    return (bal(start - timedelta(days=1), opening), cov(start, end), *txns, bal(end, closing))


OCT5, OCT20, NOV5 = date(2026, 10, 5), date(2026, 10, 20), date(2026, 11, 5)


def test_partial_sell_is_fifo_and_conserves_cost() -> None:
    facts = mf_statement(
        date(2026, 10, 1),
        date(2026, 10, 31),
        "0",
        "15",
        buy(OCT5, "10", "1000"),
        buy(OCT20, "10", "1300"),
        sell(date(2026, 10, 25), "5", "700"),
    )
    tl = Book.of(facts).timeline(FOLIO, FUND)
    assert [c.status for c in tl.checks] == ["match", "match"]
    assert tl.qty == q("15")
    assert [(lot.qty, lot.cost) for lot in tl.lots] == [(q("5"), rs("500")), (q("10"), rs("1300"))]
    (d,) = tl.disposals
    assert (d.cost, d.proceeds, d.gain) == (rs("500"), rs("700"), rs("200"))


def test_two_sips_on_one_day_both_count() -> None:
    facts = mf_statement(
        date(2026, 10, 1),
        date(2026, 10, 31),
        "0",
        "20",
        buy(OCT5, "10", "1000", nth=0),
        buy(OCT5, "10", "1000", nth=1),
    )
    assert Book.of(facts).timeline(FOLIO, FUND).qty == q("20")


def test_overlapping_statements_converge() -> None:
    sept_oct = mf_statement(
        date(2026, 9, 1), date(2026, 10, 31), "0", "10", buy(OCT5, "10", "1000")
    )
    oct_nov = mf_statement(
        date(2026, 10, 1),
        date(2026, 11, 30),
        "0",
        "15",
        buy(OCT5, "10", "1000"),
        buy(NOV5, "5", "600"),
    )
    a = Book.of(sept_oct + oct_nov)
    b = Book.of(oct_nov + sept_oct + sept_oct)
    assert a.timeline(FOLIO, FUND) == b.timeline(FOLIO, FUND)
    assert a.timeline(FOLIO, FUND).qty == q("15")
    assert reconcile(a).ok


def test_gap_inside_coverage_is_a_mismatch() -> None:
    facts = mf_statement(date(2026, 10, 1), date(2026, 10, 31), "0", "12", buy(OCT5, "10", "1000"))
    report = reconcile(Book.of(facts))
    assert not report.ok
    (m,) = report.mismatches
    assert (m.stated, m.computed) == (q("12"), q("10"))


def test_mf_opening_balance_is_inferred_with_unknown_cost() -> None:
    facts = mf_statement(date(2026, 10, 1), date(2026, 10, 31), "50", "60", buy(OCT5, "10", "1000"))
    book = Book.of(facts)
    tl = book.timeline(FOLIO, FUND)
    assert [c.status for c in tl.checks] == ["inferred", "match"]
    assert tl.lots[0].cost is None
    assert book.holdings()[0].cost is None  # unknown, never zero
    assert reconcile(book).ok


def test_balance_only_demat_and_sold_out_via_complete() -> None:
    sep30, oct31 = date(2026, 9, 30), date(2026, 10, 31)
    facts: tuple[Fact, ...] = (
        bal(sep30, "100", DEMAT, INFY),
        Complete(account=DEMAT, on=sep30),
        Complete(account=DEMAT, on=oct31),  # October eCAS no longer lists INFY
    )
    book = Book.of(facts)
    tl = book.timeline(DEMAT, INFY)
    assert [c.status for c in tl.checks] == ["inferred", "inferred"]
    assert tl.qty == q("0")
    assert tl.disposals[0].proceeds is None
    assert book.holdings() == ()


def test_split_and_bonus() -> None:
    facts: tuple[Fact, ...] = (
        buy(OCT5, "3", "3000", acct=DEMAT, isin=INFY),
        Txn(account=DEMAT, isin=INFY, on=OCT20, event=Split(num=2, den=1)),
        Txn(account=DEMAT, isin=INFY, on=NOV5, event=Bonus(units=q("6"))),
    )
    tl = Book.of(facts).timeline(DEMAT, INFY)
    assert tl.qty == q("12")
    assert [(lot.qty, lot.cost) for lot in tl.lots] == [(q("6"), rs("3000")), (q("6"), rs("0"))]


def test_split_remainder_keeps_lots_summing_to_qty() -> None:
    facts: tuple[Fact, ...] = (
        buy(OCT5, "0.000001", "1", acct=DEMAT, isin=INFY),
        buy(OCT20, "0.000001", "1", acct=DEMAT, isin=INFY),
        Txn(account=DEMAT, isin=INFY, on=NOV5, event=Split(num=3, den=2)),
    )
    tl = Book.of(facts).timeline(DEMAT, INFY)
    assert sum(lot.qty.micros for lot in tl.lots) == tl.qty.micros == 3


def test_oversell_with_no_statement_coverage_is_inferred() -> None:
    """No Coverage says the txn list is complete, so the missing buy is inferred."""
    facts = (buy(OCT5, "5", "500"), sell(OCT20, "8", "900"))
    book = Book.of(facts)
    tl = book.timeline(FOLIO, FUND)
    assert tl.qty == q("0")
    assert reconcile(book).ok
    assert any(isinstance(m, Inferred) and m.delta.micros == 3_000_000 for m in tl.movements)


def test_positions_group_accounts_and_resolve_symbols() -> None:
    other = account_id("demat", "IN300000", "20000002")
    book = Book.of(
        (
            buy(OCT5, "5", "7000", acct=DEMAT, isin=INFY),
            buy(OCT20, "3", "4500", acct=other, isin=INFY),
        ),
        instruments=(
            Instrument(
                isin=INFY, name="Infosys", kind="stock", asset_class="equity", symbol="INFY"
            ),
        ),
    )
    (pos,) = book.positions()
    assert (pos.qty, pos.cost, len(pos.holdings)) == (q("8"), rs("11500"), 2)
    assert book.resolve("infy") == INFY
    assert book.resolve(INFY) == INFY


def test_as_of_time_travel() -> None:
    facts = (buy(OCT5, "10", "1000"), buy(NOV5, "5", "600"))
    assert Book.of(facts, as_of=date(2026, 10, 31)).timeline(FOLIO, FUND).qty == q("10")


def test_thesis_latest_wins_and_journal_keeps_history() -> None:
    old = Thesis(
        isin=INFY, why="v1", invalidate_below=Price.rupees("1300"), recorded_on=date(2026, 9, 1)
    )
    new = Thesis(
        isin=INFY, why="v2", invalidate_below=Price.rupees("1350"), recorded_on=date(2026, 11, 1)
    )
    book = Book.of((new, old))
    assert book.thesis(INFY) == new
    assert book.journal(INFY) == (old, new)


def test_manual_assets_current_only() -> None:
    facts: tuple[Fact, ...] = (
        StatedBalance(asset="epf", balance=rs("800000"), as_of=date(2026, 9, 30)),
        StatedBalance(asset="epf", balance=rs("845000"), as_of=date(2026, 10, 31)),
        GoldHolding(grams=q("20"), as_of=date(2026, 10, 31)),
        FixedDeposit(
            label="sbi-1",
            principal=rs("200000"),
            rate=percent("7.1"),
            start=date(2026, 4, 1),
            months=12,
        ),
        FixedDeposit(
            label="old",
            principal=rs("1000"),
            rate=percent("6"),
            start=date(2024, 1, 31),
            months=1,
        ),
    )
    assets = Book.of(facts).manual_assets(today=date(2026, 11, 17))
    assert len(assets) == 3
    epf, gold, fd = assets
    assert isinstance(epf, StatedBalance) and epf.balance == rs("845000")
    assert isinstance(gold, GoldHolding)
    assert isinstance(fd, FixedDeposit) and fd.label == "sbi-1"  # the matured FD is gone


def test_same_day_buys_do_not_depend_on_input_order() -> None:
    lump = buy(OCT5, "10", "1000")
    sip = buy(OCT5, "10", "2000")
    out = sell(OCT20, "10", "2500")
    results = {
        (Book.of(order).holdings(), Book.of(order).timeline(FOLIO, FUND).disposals)
        for order in permutations((lump, sip, out))
    }
    assert len(results) == 1


def test_uncovered_oversell_infers_the_missing_buy() -> None:
    jan31, feb10, feb28 = date(2026, 1, 31), date(2026, 2, 10), date(2026, 2, 28)
    facts = (
        bal(jan31, "100", DEMAT, INFY),
        sell(feb10, "150", "15000", acct=DEMAT, isin=INFY),
        bal(feb28, "0", DEMAT, INFY),
    )
    book = Book.of(facts)
    tl = book.timeline(DEMAT, INFY)
    assert reconcile(book).ok
    assert [c.status for c in tl.checks] == ["inferred", "match"]
    assert sum(d.proceeds.paise for d in tl.disposals if d.proceeds) == rs("15000").paise


def test_covered_oversell_is_a_mismatch_with_held_proceeds_kept() -> None:
    facts = (
        *mf_statement(
            date(2026, 10, 1),
            date(2026, 10, 31),
            "0",
            "0",
            buy(OCT5, "5", "500"),
            sell(OCT20, "8", "800"),
        ),
    )
    book = Book.of(facts)
    (m,) = reconcile(book).mismatches
    assert (m.kind, m.stated, m.computed) == ("oversell", q("8"), q("5"))
    (d,) = book.timeline(FOLIO, FUND).disposals
    assert d.proceeds == rs("500")


def test_reverse_split_drops_empty_lots() -> None:
    facts = (
        buy(OCT5, "0.000001", "1", acct=DEMAT, isin=INFY),
        bal(OCT20, "0.000011", DEMAT, INFY),
        buy(OCT20, "0.000001", "1", acct=DEMAT, isin=INFY),
        Txn(account=DEMAT, isin=INFY, on=NOV5, event=Split(num=1, den=10)),
    )
    tl = Book.of(facts).timeline(DEMAT, INFY)
    assert all(lot.qty.micros for lot in tl.lots)
    assert sum(lot.qty.micros for lot in tl.lots) == tl.qty.micros


def test_closed_position_is_not_rechecked_every_month() -> None:
    sep30, oct31, nov30 = date(2026, 9, 30), date(2026, 10, 31), date(2026, 11, 30)
    facts = (
        bal(sep30, "100", DEMAT, INFY),
        Complete(account=DEMAT, on=oct31),
        Complete(account=DEMAT, on=nov30),
    )
    assert len(Book.of(facts).timeline(DEMAT, INFY).checks) == 2


def test_balance_only_pair_later_explained_by_trades() -> None:
    sep30 = date(2026, 9, 30)
    only_balance = (bal(sep30, "10", DEMAT, INFY),)
    with_trade = (*only_balance, buy(date(2026, 9, 1), "10", "15000", acct=DEMAT, isin=INFY))
    assert Book.of(only_balance).holdings()[0].cost is None
    tl = Book.of(with_trade).timeline(DEMAT, INFY)
    assert not any(isinstance(m, Inferred) for m in tl.movements)
    assert Book.of(with_trade).holdings()[0].cost == rs("15000")


def test_blank_lookup_is_rejected() -> None:
    book = Book.of(
        (),
        instruments=(
            Instrument(isin=FUND, name="A fund", kind="mutual_fund", asset_class="equity"),
        ),
    )
    with pytest.raises(LookupError):
        book.resolve("  ")
