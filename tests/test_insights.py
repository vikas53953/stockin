"""The insight engine: sentences agree with their numbers, and the numbers
are right. Properties use the same true-history generator as the ledger's."""

from __future__ import annotations

import json
import random
import re
from datetime import date
from pathlib import Path

import cas_fixtures as fx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_properties import MONTHS, Trade, _statement, histories, windows  # type: ignore[attr-defined]
from typer.testing import CliRunner

from stockin.book import Book
from stockin.cli import app
from stockin.facts import Isin, parse_isin
from stockin.insights import (
    all_insights,
    concentration,
    cost_coverage,
    invested,
    long_term_from,
    pct,
    rupees,
    sip_discipline,
    tax_clock,
    weights,
    what_changed,
)
from stockin.money import Money, Price
from stockin.prices import PriceBook, Quote
from stockin.sources import cas

TODAY = date(2026, 11, 17)


def _book(statements: list[fx.CASData], shuffle: random.Random | None = None) -> Book:
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


# ------------------------------------------------------------- properties


@given(histories(), windows, navs)
def test_every_sentence_is_built_from_its_own_numbers(
    history: dict[str, list[Trade]], ws: list[tuple[int, int]], ps: dict[str, int]
) -> None:
    book = _book([_statement(history, a, b) for a, b in ws])
    for ins in all_insights(book, _prices(ps), TODAY):
        n = ins.numbers
        text = ins.headline + " " + ins.detail
        if ins.kind == "concentration":
            assert pct(int(n["top_bps"])) in ins.headline
            assert pct(int(n["top3_bps"])) in ins.detail
            assert sum(p.value for p in ins.series) == n["total_paise"]
            assert [p.value for p in ins.series] == sorted(
                (p.value for p in ins.series), reverse=True
            )
        elif ins.kind == "cost_coverage":
            if int(n["unknown_bps"]) > 0:
                assert pct(int(n["unknown_bps"])) in ins.headline
            assert sum(p.value for p in ins.series) == 10_000
            assert int(n["gain_paise"]) == int(n["known_value_paise"]) - int(n["known_cost_paise"])
            if int(n["known_cost_paise"]) > 0:
                # the sign is said in words ("up"/"down"); the figure is the size
                assert pct(abs(int(n["gain_bps"]))) in text
                assert ("down" in text) == (int(n["gain_paise"]) < 0)
        elif ins.kind == "what_changed":
            assert rupees(Money(paise=int(n["added_paise"]))) in ins.headline
        elif ins.kind == "sip_discipline":
            assert str(n["expected"]) in ins.headline
            assert int(n["paid"]) + int(n["missed"]) == int(n["expected"])
            assert sum(p.value for p in ins.series) == n["paid"]
        elif ins.kind == "invested":
            assert rupees(Money(paise=abs(int(n["net_paise"])))) in ins.headline
            assert str(n["fy"]) in ins.headline
        # numbers never appear in a sentence unless they came from `numbers`
        for found in re.findall(r"\d+\.\d%", text):
            sizes = [abs(int(v)) for k, v in n.items() if k.endswith("bps")]
            assert any(pct(v) == found for v in sizes), found


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


def _sip_history(months: list[int]) -> dict[str, list[Trade]]:
    from decimal import Decimal

    return {
        fx.HDFC_MF: [Trade(m, 5, True, Decimal("10.000"), Decimal("1000.00"), None) for m in months]
    }


def test_a_missed_sip_month_is_counted() -> None:
    book = _book([_statement(_sip_history([1, 2, 4, 5]), 1, 5)])
    ins = sip_discipline(book)
    assert ins is not None
    assert ins.numbers == {"expected": 5, "paid": 4, "missed": 1, "sips": 1}
    assert ins.tone == "warn"
    assert ins.headline == "1 of 5 SIP instalments missed across 1 SIP."


def test_two_purchases_are_not_a_sip() -> None:
    assert sip_discipline(_book([_statement(_sip_history([1, 3]), 1, 3)])) is None


def test_long_term_starts_the_day_after_the_first_anniversary() -> None:
    assert long_term_from(date(2026, 1, 5)) == date(2027, 1, 6)
    assert long_term_from(date(2024, 2, 29)) == date(2025, 3, 2)


def test_tax_clock_names_the_next_known_lot_and_skips_unknown_dates() -> None:
    history = _sip_history([1, 2, 3])
    # statement from March only: Jan and Feb units arrive as an opening balance
    book = _book([_statement(history, 3, 3)])
    ins = tax_clock(book, TODAY)
    assert ins is not None
    assert ins.numbers["date"] == "2027-03-06"  # the March lot, not the unknown-date opening units
    assert ins.headline.startswith("₹1,000 (at cost) of HDFC Flexi Cap")
    assert tax_clock(book, date(2027, 3, 6)) is None  # already long-term


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


def test_concentration_tone_follows_the_25_percent_line() -> None:
    from decimal import Decimal

    one = {fx.HDFC_MF: [Trade(1, 5, True, Decimal("10.000"), Decimal("1000.00"), None)]}
    book = _book([_statement(one, 1, 1)])
    ins = concentration(book, _prices({fx.HDFC_MF: 100_000_000}))
    assert ins is not None
    assert ins.tone == "warn"
    assert ins.headline == "One holding is 100.0% of your money."
    assert concentration(book, PriceBook(quotes={})) is None
    cov = cost_coverage(book, _prices({fx.HDFC_MF: 150_000_000}))
    assert cov is not None
    assert cov.headline == "You're up ₹500 (50.0%) on what you hold."


def test_pct_rounds_half_even_to_one_decimal() -> None:
    assert pct(8183) == "81.8%"
    assert pct(8185) == "81.8%"  # 818.5 tenths -> 818 (even)
    assert pct(8195) == "82.0%"
    assert pct(-25) == "-0.2%"


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
