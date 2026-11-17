"""The insight engine: sentences agree with their numbers, and the numbers
are right. Properties use the same true-history generator as the ledger's."""

from __future__ import annotations

import json
import random
import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import cas_fixtures as fx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_properties import (  # type: ignore[attr-defined]
    MONTHS,
    STOCKS,
    Trade,
    _demat,
    _statement,
    demat_months,
    histories,
    windows,
)
from typer.testing import CliRunner

from stockin.book import Book
from stockin.cli import app
from stockin.facts import Isin, parse_isin
from stockin.insights import (
    Insight,
    all_insights,
    concentration,
    cost_coverage,
    invested,
    long_term_from,
    pct,
    rupees,
    sip_discipline,
    tax_clock,
    tenths_of,
    weights,
    what_changed,
)
from stockin.money import Money, Price, Qty, div_half_even
from stockin.prices import PriceBook, Quote
from stockin.sources import cas

TODAY = date(2026, 11, 17)


def _book(
    statements: Sequence[fx.CASData | fx.NSDLCASData], shuffle: random.Random | None = None
) -> Book:
    facts, insts, accts = [], {}, {}
    for s in statements:
        st_ = cas.from_casparser(s, "sha")
        facts.extend(st_.facts)
        insts.update({i.isin: i for i in st_.instruments})
        accts.update({a.id: a for a in st_.accounts})
    if shuffle is not None:
        shuffle.shuffle(facts)
    return Book.of(tuple(facts), tuple(insts.values()), tuple(accts.values()))


def _prices(navs: dict[str, int]) -> PriceBook:
    return PriceBook.best(
        Quote(isin=parse_isin(i), on=TODAY, price=Price(micro_rupees=p), source="amfi")
        for i, p in navs.items()
    )


nav = st.integers(1_000_000, 5_000 * 1_000_000)  # ₹1 to ₹5,000 a unit
navs = st.fixed_dictionaries({fx.HDFC_MF: nav, fx.ICICI_MF: nav})
ALL = (fx.HDFC_MF, fx.ICICI_MF, *STOCKS)
# some holdings may have no price: each ISIN is priced or not
some_navs = st.fixed_dictionaries({}, optional={i: nav for i in ALL})

_RUPEES = re.compile(r"₹[\d,]+(?:\.\d\d)?")
_PCT = re.compile(r"\d+\.\d%")
_DATE = re.compile(r"\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}")


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')} {d.year}"


def _model(book: Book, ps: dict[str, int]) -> tuple[list[tuple[int, int | None]], int]:
    """An independent count: (value, cost or None) per priced ISIN, largest
    first, and how many held ISINs have no price."""
    units: dict[str, int] = {}
    costs: dict[str, int | None] = {}
    for h in book.holdings():
        units[h.isin] = units.get(h.isin, 0) + h.qty.micros
        prev = costs.get(h.isin, 0)
        costs[h.isin] = None if prev is None or h.cost is None else prev + h.cost.paise
    rows = [
        (Money.of(Qty(micros=u), Price(micro_rupees=ps[i])).paise, costs[i])
        for i, u in units.items()
        if i in ps
    ]
    return sorted(rows, key=lambda r: -r[0]), sum(1 for i in units if i not in ps)


def _check_sentence_numbers(ins: Insight) -> None:
    """Every %, ₹ amount, date and day count in the words is one of `numbers`."""
    n = ins.numbers
    text = ins.headline + " " + ins.detail
    tenths = [abs(int(v)) for k, v in n.items() if k.endswith("_tenths")]
    for found in _PCT.findall(text):
        assert any(pct(v) == found for v in tenths), (found, ins)
    paise = [abs(int(v)) for k, v in n.items() if k.endswith("_paise")]
    for found in _RUPEES.findall(text):
        assert any(rupees(Money(paise=v)) == found for v in paise), (found, ins)
    dates = [_day(str(v)) for k, v in n.items() if k in ("start", "end", "date")]
    for found in _DATE.findall(text):
        assert found in dates, (found, ins)
    for found in re.findall(r"in (\d+) days?", text):
        assert int(found) == n["days"], (found, ins)


# ------------------------------------------------------------- properties


@given(histories(), demat_months(), windows, some_navs)
def test_every_sentence_is_built_from_its_own_numbers(
    history: dict[str, list[Trade]],
    held: list[dict[str, int]],
    ws: list[tuple[int, int]],
    ps: dict[str, int],
) -> None:
    book = _book([_statement(history, a, b) for a, b in ws] + [_demat(held, a, b) for a, b in ws])
    for ins in all_insights(book, _prices(ps), TODAY):
        _check_sentence_numbers(ins)
        n = ins.numbers
        if ins.kind == "tax_clock":
            # the headline's money is the chart's first bar, on the same date
            assert ins.series[0].value == n["cost_paise"]
            assert ins.series[0].key == n["date"]
            assert [p.key for p in ins.series] == sorted(p.key for p in ins.series)
        elif ins.kind == "sip_discipline":
            assert int(n["paid"]) + int(n["missed"]) == int(n["expected"])
            assert sum(p.value for p in ins.series) == n["paid"]
            assert len(ins.series) == n["expected"]
        elif ins.kind == "what_changed":
            assert ins.series[0].value == n["added_paise"]
            assert ins.series[1].value == -int(n["out_paise"])
        elif ins.kind == "invested":
            assert any(p.value == n["net_paise"] for p in ins.series)


@given(histories(), demat_months(), windows, some_navs)
def test_value_insights_match_an_independent_count(
    history: dict[str, list[Trade]],
    held: list[dict[str, int]],
    ws: list[tuple[int, int]],
    ps: dict[str, int],
) -> None:
    book = _book([_statement(history, a, b) for a, b in ws] + [_demat(held, a, b) for a, b in ws])
    rows, unpriced = _model(book, ps)
    total = sum(v for v, _ in rows)
    conc = concentration(book, _prices(ps))
    cov = cost_coverage(book, _prices(ps))
    if not rows or total <= 0:
        assert conc is None and cov is None
        return
    assert conc is not None and cov is not None
    top, top3 = rows[0][0], sum(v for v, _ in rows[:3])
    c = conc.numbers
    assert c["top_tenths"] == div_half_even(top * 1_000, total)
    assert c["top3_tenths"] == div_half_even(top3 * 1_000, total)
    assert c["fall10_tenths"] == div_half_even(top * 100, total)
    assert (c["unpriced"], c["holdings"]) == (unpriced, len(rows))
    alone = len(rows) == 1 or rows[1][0] < top
    assert conc.headline.startswith("One holding is") == (top * 4 > total and alone)
    assert ("whole portfolio" in conc.detail) == (unpriced == 0)
    assert (conc.tone == "warn") == (top * 4 > total or (len(rows) > 3 and top3 * 2 > total))
    assert ("of what's priced" in conc.headline) == (unpriced > 0)
    assert ("Your top 3" in conc.detail) == (len(rows) > 3)
    assert ("no price yet" in conc.detail) == (unpriced > 0)

    known = [(v, k) for v, k in rows if k is not None]
    kv, kc = sum(v for v, _ in known), sum(k for _, k in known)
    unknown = total - kv
    g = cov.numbers
    assert (g["known_value_paise"], g["known_cost_paise"]) == (kv, kc)
    assert g["unknown_tenths"] == div_half_even(unknown * 1_000, total)
    assert g["gain_paise"] == kv - kc
    text = cov.headline + " " + cov.detail
    if kv - kc > 0:
        assert " up " in text and " down " not in text
    elif kv - kc < 0:
        assert " down " in text and " up " not in text
    elif known:
        assert "even" in text
    if kc > 0 and kv != kc:
        assert g["gain_tenths"] == div_half_even(abs(kv - kc) * 1_000, kc)
    if kc == 0:
        assert "gain_tenths" not in g or kv == kc  # no percent of a zero cost
    # an unknown-cost reason only when some priced value has no cost
    assert cov.headline.startswith("Gain is unknown") == (unknown > 0)
    assert ("statements" in cov.detail) == (unknown > 0)
    assert ("no price yet" in cov.detail) == (unpriced > 0)
    if unknown == 0:
        expect = "good" if kv > kc else "warn" if kv < kc else "info"
        assert cov.tone == expect


@given(histories(), windows, navs, st.randoms(use_true_random=False))
def test_insights_do_not_depend_on_fact_order(
    history: dict[str, list[Trade]],
    ws: list[tuple[int, int]],
    ps: dict[str, int],
    rnd: random.Random,
) -> None:
    statements = [_statement(history, a, b) for a, b in ws]
    assert all_insights(_book(statements), _prices(ps), TODAY) == all_insights(
        _book(statements, shuffle=rnd), _prices(ps), TODAY
    )


@given(histories(), navs)
def test_weights_add_up_to_the_priced_total(
    history: dict[str, list[Trade]], ps: dict[str, int]
) -> None:
    book = _book([_statement(history, 1, MONTHS)])
    ws, total, unpriced = weights(book, _prices(ps))
    assert sum(w.value.paise for w in ws) == total.paise
    assert not unpriced
    if total.paise:
        assert abs(sum(w.bps for w in ws) - 10_000) <= len(ws)  # each share rounds once


# ------------------------------------------------------------------ units


def _trade(month: int, day: int, buy: bool = True, rupees_: str = "1000.00") -> Trade:
    from decimal import Decimal

    return Trade(month, day, buy, Decimal("10.000" if buy else "1.000"), Decimal(rupees_), None)


def _sip_history(months: list[int], day: int = 5) -> dict[str, list[Trade]]:
    return {fx.HDFC_MF: [_trade(m, day) for m in months]}


def test_a_missed_sip_month_is_counted() -> None:
    book = _book([_statement(_sip_history([1, 2, 4, 5]), 1, 5)])
    ins = sip_discipline(book)
    assert ins is not None
    assert ins.numbers == {"expected": 5, "paid": 4, "missed": 1, "sips": 1, "stopped": 0}
    assert ins.tone == "warn"
    assert ins.headline == "1 of 5 SIP instalments missed across 1 SIP."


def test_a_missed_last_month_counts_once_the_statement_passes_the_sip_day() -> None:
    # SIP on the 5th: Jan to Mar paid, Apr not, statement to 30 Apr
    book = _book([_statement(_sip_history([1, 2, 3]), 1, 4)])
    ins = sip_discipline(book)
    assert ins is not None
    assert (ins.numbers["expected"], ins.numbers["missed"]) == (4, 1)


def test_the_statement_month_is_not_due_before_the_sip_day() -> None:
    # SIP on the 20th, Jan to Mar paid; a statement to 10 Apr: April isn't due yet
    history = {fx.HDFC_MF: [_trade(m, 20) for m in (1, 2, 3)]}
    stmt = _statement(history, 1, 4)
    stmt = stmt.model_copy(
        update={"statement_period": stmt.statement_period.model_copy(update={"to": "10-Apr-2026"})}
    )
    ins = sip_discipline(_book([stmt]))
    assert ins is not None
    assert ins.numbers["missed"] == 0
    assert ins.headline == "All 3 SIP instalments paid across 1 SIP."


def test_a_sip_with_three_quiet_months_is_stopped_not_missed() -> None:
    book = _book([_statement(_sip_history([1, 2, 3]), 1, 6)])
    ins = sip_discipline(book)
    assert ins is not None
    assert (ins.numbers["expected"], ins.numbers["missed"], ins.numbers["stopped"]) == (3, 0, 1)
    assert "HDFC Flexi Cap Fund looks stopped" in ins.detail


def test_a_sold_out_sip_is_due_only_to_its_last_purchase() -> None:
    history = {fx.HDFC_MF: [_trade(1, 5), _trade(2, 5), _trade(3, 5)]}
    history[fx.HDFC_MF].append(_trade(4, 6, buy=False, rupees_="1000.00"))
    from decimal import Decimal

    history[fx.HDFC_MF][-1] = Trade(4, 6, False, Decimal("30.000"), Decimal("1000.00"), None)
    ins = sip_discipline(_book([_statement(history, 1, 5)]))
    assert ins is not None
    assert (ins.numbers["expected"], ins.numbers["missed"]) == (3, 0)


def test_dividend_reinvestments_are_not_sip_instalments_or_new_money() -> None:
    rows = [
        fx.txn("2026-01-05", "PURCHASE", "10", "1000"),
        fx.txn("2026-02-05", "PURCHASE", "10", "1000"),
        fx.txn("2026-03-25", "DIVIDEND_REINVEST", "1", "90"),
    ]
    stmt = fx.rta(
        "01-Jan-2026",
        "31-Mar-2026",
        [
            fx.folio(
                "9000001/01", "Test Mutual Fund", [fx.scheme(fx.HDFC_MF, "HDFC", "0", "21", rows)]
            )
        ],
    )
    book = _book([stmt])
    assert sip_discipline(book) is None  # 2 months of real purchases, not 3
    ins = what_changed(book)
    assert ins is not None
    assert (ins.numbers["added_paise"], ins.numbers["income_paise"]) == (200_000, 0)
    assert ins.numbers["reinvested_paise"] == 9_000


def _rta(rows: list[dict[str, str | None]], close: str, start: str, end: str) -> fx.CASData:
    scheme = fx.scheme(fx.HDFC_MF, "HDFC Flexi Cap Fund", "0", close, rows)  # type: ignore[arg-type]
    return fx.rta(start, end, [fx.folio("9000001/01", "Test Mutual Fund", [scheme])])


def test_a_payout_or_reinvestment_on_the_sip_day_keeps_the_sip_purchase() -> None:
    rows = [
        fx.txn("2026-01-05", "PURCHASE", "10", "1000"),
        fx.txn("2026-02-05", "PURCHASE", "10", "1000"),
        fx.txn("2026-03-05", "PURCHASE", "10", "1000"),
        fx.txn("2026-03-05", "DIVIDEND_PAYOUT", None, "50"),  # IDCW paid out, same day
        fx.txn("2026-04-05", "PURCHASE", "10", "1000"),
        fx.txn("2026-04-05", "DIVIDEND_REINVEST", "0.5", "50"),  # and reinvested, same day
    ]
    book = _book([_rta(rows, "40.5", "01-Jan-2026", "30-Apr-2026")])
    sip = sip_discipline(book)
    assert sip is not None
    assert (sip.numbers["expected"], sip.numbers["missed"]) == (4, 0)
    ch = what_changed(book)
    assert ch is not None
    assert ch.numbers["added_paise"] == 400_000  # the 4 SIP purchases, not the reinvestment
    assert (ch.numbers["income_paise"], ch.numbers["reinvested_paise"]) == (5_000, 5_000)


def test_months_between_statements_are_not_missed() -> None:
    history = _sip_history([1, 2, 3, 7, 8, 9])
    book = _book([_statement(history, 1, 3), _statement(history, 7, 9)])
    ins = sip_discipline(book)
    assert ins is not None
    assert (ins.numbers["expected"], ins.numbers["missed"]) == (6, 0)


def test_a_statement_ending_on_the_sip_day_makes_that_month_due() -> None:
    history = {fx.HDFC_MF: [_trade(m, 20) for m in (1, 2, 3)]}
    stmt = _statement(history, 1, 4)
    stmt = stmt.model_copy(
        update={"statement_period": stmt.statement_period.model_copy(update={"to": "20-Apr-2026"})}
    )
    ins = sip_discipline(_book([stmt]))
    assert ins is not None
    assert (ins.numbers["expected"], ins.numbers["missed"]) == (4, 1)


def test_only_mutual_funds_count_as_sips() -> None:
    st_ = cas.from_casparser(_statement(_sip_history([1, 2, 3]), 1, 3), "sha")
    as_stock = tuple(i.model_copy(update={"kind": "stock"}) for i in st_.instruments)
    fund = Book.of(tuple(st_.facts), tuple(st_.instruments), tuple(st_.accounts))
    stock = Book.of(tuple(st_.facts), as_stock, tuple(st_.accounts))
    assert sip_discipline(fund) is not None
    assert sip_discipline(stock) is None


def test_two_purchases_are_not_a_sip() -> None:
    assert sip_discipline(_book([_statement(_sip_history([1, 3]), 1, 3)])) is None


def test_long_term_starts_the_day_after_the_first_anniversary() -> None:
    assert long_term_from(date(2026, 1, 5)) == date(2027, 1, 6)
    assert long_term_from(date(2024, 2, 28)) == date(2025, 3, 1)
    assert long_term_from(date(2024, 2, 29)) == date(2025, 3, 1)  # 366 days, like the rest
    assert (long_term_from(date(2025, 1, 5)) - date(2025, 1, 5)).days == 366


def test_tax_clock_names_the_next_known_lot_and_skips_unknown_dates() -> None:
    history = _sip_history([1, 2, 3])
    # statement from March only: Jan and Feb units arrive as an opening balance
    book = _book([_statement(history, 3, 3)])
    ins = tax_clock(book, TODAY)
    assert ins is not None
    assert ins.numbers["date"] == "2027-03-06"  # the March lot, not the unknown-date opening units
    assert ins.headline.startswith("₹1,000 (at cost) of HDFC Flexi Cap")
    assert tax_clock(book, date(2027, 3, 6)) is None  # already long-term


def test_tax_clock_takes_the_next_date_and_charts_cost_per_date() -> None:
    book = _book([_statement({fx.HDFC_MF: [_trade(1, 5), _trade(1, 9, rupees_="300.00")]}, 1, 2)])
    ins = tax_clock(book, date(2026, 12, 1))
    assert ins is not None
    assert ins.numbers["date"] == "2027-01-06"  # the earlier of 6 and 10 Jan
    assert [(p.key, p.value) for p in ins.series] == [
        ("2027-01-06", 100_000),
        ("2027-01-10", 30_000),
    ]
    assert ins.detail.startswith("That's in 36 days.")
    one = tax_clock(book, date(2027, 1, 5))
    assert one is not None and one.detail.startswith("That's in 1 day.")


def test_tax_clock_is_equity_only() -> None:
    st_ = cas.from_casparser(_statement({fx.ICICI_MF: [_trade(1, 5)]}, 1, 1), "sha")
    insts = tuple(i.model_copy(update={"asset_class": "debt"}) for i in st_.instruments)
    equity = Book.of(tuple(st_.facts), tuple(st_.instruments), tuple(st_.accounts))
    debt = Book.of(tuple(st_.facts), insts, tuple(st_.accounts))
    assert tax_clock(equity, TODAY) is not None
    assert tax_clock(debt, TODAY) is None


def test_invested_is_net_by_financial_year() -> None:
    from decimal import Decimal

    history = {
        fx.HDFC_MF: [
            Trade(3, 31, True, Decimal("1.000"), Decimal("100.00"), None),
            Trade(4, 1, True, Decimal("1.000"), Decimal("250.00"), None),
            Trade(5, 2, False, Decimal("0.500"), Decimal("40.00"), None),
        ]
    }
    ins = invested(_book([_statement(history, 1, 6)]))
    assert ins is not None
    assert [(p.label, p.value) for p in ins.series] == [
        ("FY 2025-26", 10_000),
        ("FY 2026-27", 21_000),
    ]
    assert ins.headline == "You invested ₹210 (net) in FY 2026-27."


def test_what_changed_covers_only_the_newest_statement_months() -> None:
    history = _sip_history([1, 2, 3])
    book = _book([_statement(history, 1, 2), _statement(history, 1, 3)])
    ins = what_changed(book)
    assert ins is not None
    assert (ins.numbers["start"], ins.numbers["end"]) == ("2026-03-01", "2026-03-31")
    assert ins.numbers["added_paise"] == 100_000
    assert ins.headline == "From 1 Mar 2026 to 31 Mar 2026: you added ₹1,000."


def test_what_changed_counts_sales_and_starts_at_the_statement_start() -> None:
    from decimal import Decimal

    history = {
        fx.HDFC_MF: [
            _trade(2, 10),
            Trade(2, 20, False, Decimal("4.000"), Decimal("450.00"), None),
        ]
    }
    ins = what_changed(_book([_statement(history, 1, 2)]))
    assert ins is not None
    assert ins.numbers["start"] == "2026-01-01"
    assert (ins.numbers["out_paise"], ins.numbers["sells"]) == (45_000, 1)
    assert ins.headline == "From 1 Jan 2026 to 28 Feb 2026: you added ₹1,000 and took out ₹450."
    assert ins.detail.startswith("1 purchase, 1 sale.")


def test_concentration_tone_follows_the_25_percent_line() -> None:
    book = _book([_statement({fx.HDFC_MF: [_trade(1, 5)]}, 1, 1)])
    ins = concentration(book, _prices({fx.HDFC_MF: 100_000_000}))
    assert ins is not None
    assert ins.tone == "warn"
    assert ins.headline == "One holding is 100.0% of your money."
    assert "Your top 3" not in ins.detail  # one holding has no "top 3"
    assert concentration(book, PriceBook(quotes={})) is None
    cov = cost_coverage(book, _prices({fx.HDFC_MF: 150_000_000}))
    assert cov is not None
    assert cov.headline == "You're up ₹500 (50.0%) on what you hold."
    even = cost_coverage(book, _prices({fx.HDFC_MF: 100_000_000}))
    assert even is not None
    assert even.headline == "You're even on what you hold." and even.tone == "info"


def test_a_holding_without_a_price_is_named_not_counted_as_unknown_cost() -> None:
    buy = _trade(1, 5)
    book = _book([_statement({fx.HDFC_MF: [buy], fx.ICICI_MF: [buy]}, 1, 1)])
    ps = _prices({fx.HDFC_MF: 110_000_000})  # ICICI has no price
    conc = concentration(book, ps)
    cov = cost_coverage(book, ps)
    assert conc is not None and cov is not None
    assert conc.headline == "One holding is 100.0% of what's priced."
    assert "1 holding (ICICI Bluechip) has no price yet and is left out." in conc.detail
    assert cov.headline == "You're up ₹100 (10.0%) on what's priced."
    assert "statements start after" not in cov.detail


def test_exactly_25_percent_is_not_over_the_line() -> None:
    buy = _trade(1, 5)
    mf = {fx.HDFC_MF: [buy], fx.ICICI_MF: [buy]}
    shares: list[dict[str, int]] = [{i: 100 for i in STOCKS[:2]}] * MONTHS
    book = _book([_statement(mf, 1, 1), _demat(shares, 1, 1)])
    every: dict[str, int] = {i: 1_000 * 1_000_000 for i in (fx.HDFC_MF, fx.ICICI_MF)}
    every |= {i: 100 * 1_000_000 for i in STOCKS[:2]}  # 4 x Rs 10,000
    ins = concentration(book, _prices(every))
    assert ins is not None
    assert ins.headline == "Your largest holding is 25.0% of your money."
    assert ins.tone == "warn"  # the top 3 (75%) are over half


def _five_holdings() -> Book:
    buy = _trade(1, 5)
    mf = {fx.HDFC_MF: [buy], fx.ICICI_MF: [buy]}
    shares: list[dict[str, int]] = [{i: 100 for i in STOCKS}] * MONTHS
    return _book([_statement(mf, 1, 1), _demat(shares, 1, 1)])


def test_with_a_holding_unpriced_every_share_says_priced() -> None:
    ps: dict[str, int] = {fx.HDFC_MF: 3_000 * 1_000_000}  # Rs 30,000
    ps |= {i: 100 * 1_000_000 for i in STOCKS}  # 3 x Rs 10,000; ICICI unpriced
    ins = concentration(_five_holdings(), _prices(ps))
    assert ins is not None
    assert ins.headline == "One holding is 50.0% of what's priced."
    assert "Your top 3 priced holdings are 83.3% together." in ins.detail
    assert "falls 10%, what's priced falls about 5.0%." in ins.detail
    assert "whole portfolio" not in ins.detail


def test_equal_largest_holdings_are_not_called_one_holding() -> None:
    ps: dict[str, int] = {i: 100 * 1_000_000 for i in STOCKS}  # 3 x Rs 10,000
    shares: list[dict[str, int]] = [{i: 100 for i in STOCKS}] * MONTHS
    ins = concentration(_book([_demat(shares, 1, 1)]), _prices(ps))
    assert ins is not None
    assert ins.headline == "Your largest holding is 33.3% of your money."
    assert ins.tone == "warn"


def test_unknown_cost_advice_depends_on_where_the_units_are() -> None:
    shares: list[dict[str, int]] = [{i: 100 for i in STOCKS}] * MONTHS
    ps: dict[str, int] = {i: 100 * 1_000_000 for i in STOCKS}
    demat = cost_coverage(_book([_demat(shares, 1, 1)]), _prices(ps))
    assert demat is not None
    assert "Demat statements (NSDL, CDSL) list what you hold" in demat.detail
    assert "first investment" not in demat.detail
    # a fund statement from March: Jan and Feb units are an opening balance
    fund = cost_coverage(
        _book([_statement(_sip_history([1, 2, 3]), 3, 3)]), _prices({fx.HDFC_MF: 100_000_000})
    )
    assert fund is not None
    assert "A statement from your first investment fixes this." in fund.detail
    assert "Demat" not in fund.detail


def test_one_holding_at_exactly_25_percent_is_not_over_the_line() -> None:
    ps: dict[str, int] = {fx.HDFC_MF: 2_500 * 1_000_000, fx.ICICI_MF: 1_875 * 1_000_000}
    ps |= {i: 187_500_000 for i in STOCKS}  # Rs 25,000 then 4 x Rs 18,750
    ins = concentration(_five_holdings(), _prices(ps))
    assert ins is not None
    assert ins.headline == "Your largest holding is 25.0% of your money."
    nudge = concentration(_five_holdings(), _prices(ps | {fx.HDFC_MF: 2_500_010_000}))
    assert nudge is not None and nudge.headline.startswith("One holding is 25.0%")


def test_the_headline_share_rounds_once_from_the_money() -> None:
    buy = _trade(1, 5)
    book = _book([_statement({fx.HDFC_MF: [buy], fx.ICICI_MF: [buy]}, 1, 1)])
    # 8,185.49 of 10,000.00: 81.8549% -> 81.9% (via 8185 bps it would be 81.8%)
    ins = concentration(book, _prices({fx.HDFC_MF: 818_549_000, fx.ICICI_MF: 181_451_000}))
    assert ins is not None
    assert ins.headline == "One holding is 81.9% of your money."


def test_five_equal_holdings_are_not_called_one_big_holding() -> None:
    buy = _trade(1, 5)
    mf = {fx.HDFC_MF: [buy], fx.ICICI_MF: [buy]}
    shares: list[dict[str, int]] = [{i: 100 for i in STOCKS}] * MONTHS
    book = _book([_statement(mf, 1, 1), _demat(shares, 1, 1)])
    every: dict[str, int] = {i: 1_000 * 1_000_000 for i in (fx.HDFC_MF, fx.ICICI_MF)}
    every |= {i: 100 * 1_000_000 for i in STOCKS}  # 5 x Rs 10,000
    ins = concentration(book, _prices(every))
    assert ins is not None
    assert ins.headline == "Your largest holding is 20.0% of your money."
    assert ins.numbers["top3_tenths"] == 600
    assert ins.tone == "warn"  # the top 3 are over half


def test_percentages_round_once_from_the_money() -> None:
    assert pct(818) == "81.8%"
    assert pct(-2) == "-0.2%"
    assert tenths_of(97_652, 100_000) == 977  # 97.652% -> 97.7%, not 97.6% via 9765 bps
    assert tenths_of(8_185, 10_000) == 818  # 81.85% -> half-even 81.8%
    assert tenths_of(8_195, 10_000) == 820


def test_names_read_naturally() -> None:
    from stockin.insights import _names, _short_name  # pyright: ignore[reportPrivateUsage]

    assert _names(["A", "B", "C"]) == "A, B and C"
    assert _names(["A", "B"]) == "A and B"
    assert _short_name("STATE BANK OF INDIA - EQ NEW RE. 1/-") == "STATE BANK OF INDIA"


def test_insights_cli_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    runner = CliRunner()
    fixtures = Path(__file__).parent / "fixtures"
    assert (
        runner.invoke(
            app, ["import", "cas", str(fixtures / "cas" / "mf-2026-01-to-02.json")]
        ).exit_code
        == 0
    )
    out = runner.invoke(app, ["insights", "--json"])
    assert out.exit_code == 0, out.output
    view = json.loads(out.output)
    assert view["priced_on"] is None
    kinds = [i["kind"] for i in view["insights"]]
    assert kinds[0] == "what_changed"
    assert "concentration" not in kinds  # needs prices
    human = runner.invoke(app, ["insights"])
    assert "no prices yet" in human.output
    _ = Isin  # keep the import for type readers
