"""A holding's own page: the weight bar's geometry, agreement with the
dashboard and the verdict, and every dashboard link resolving."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from stockin.cli import app as cli
from stockin.facts import parse_isin
from stockin.holding import _bar, holding_page, price_text  # pyright: ignore[reportPrivateUsage]
from stockin.insights import CONCENTRATED_ONE, pct, rupees, tenths_of, weights
from stockin.money import Bps, Qty
from stockin.prices import PriceStore
from stockin.store import Ledger
from stockin.thesis import BAND, Thesis, ThesisStore, for_book
from stockin.web import create_app

FIX = Path(__file__).parent / "fixtures"
INFY = parse_isin("INE009A01021")
HDFC = parse_isin("INF179K01BE2")
runner = CliRunner()
PORT = 8765
HOST = f"http://127.0.0.1:{PORT}"
TODAY = date(2026, 11, 19)


def _t(target: int | None) -> Thesis:
    return Thesis(
        isin=INFY,
        why="a reason",
        target=Bps(target) if target is not None else None,
        written=date(2026, 11, 18),
    )


@given(st.integers(0, 10_000), st.none() | st.integers(0, 10_000))
def test_the_weight_bar_puts_weight_target_band_and_line_on_one_scale(
    w: int, target: int | None
) -> None:
    b = _bar(w, _t(target))
    marks = [b.weight, b.line] + ([b.target] if b.target is not None else [])
    assert all(0 <= m <= 100 for m in marks)
    assert (b.weight < b.line) == (w < CONCENTRATED_ONE)  # order is kept
    assert (b.weight == b.line) == (w == CONCENTRATED_ONE)
    assert b.weight_text == pct(tenths_of(w, 10_000))
    if target is None:
        assert b.band is None and b.target is None and b.band_text == ""
    else:
        assert b.band is not None and b.target is not None
        lo, hi = b.band
        assert lo <= b.target <= hi
        # inside the drawn band exactly when the verdict says hold
        assert (abs(w - target) <= BAND) == (lo <= b.weight <= hi)
        assert pct(tenths_of(target, 10_000)) in b.band_text
        # the drawn band is a full ±5 points, never clipped at the track's end
        # (verifier's Q3): both halves have the width BAND has on this scale
        scale = (b.weight / w) if w else None
        if scale and target >= BAND:
            assert abs((hi - b.target) - (b.target - lo)) < 1e-9
            assert abs((hi - lo) - 2 * BAND * scale) < 1e-6


def test_a_price_reads_exactly() -> None:
    assert price_text(1_367_000_000) == "₹1,367.00"
    assert price_text(2_378_851_000) == "₹2,378.851"
    assert price_text(94_481_700) == "₹94.4817"


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    for name in ("mf-2026-01.json", "demat-2026-02.json"):
        r = runner.invoke(cli, ["import", "cas", str(FIX / "cas" / name)])
        assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["prices", "import", str(FIX / "eod" / "bhavcopy-sample.csv")])
    assert r.exit_code in (0, 1), r.output
    return tmp_path


def _book_and_prices(data: Path):  # type: ignore[no-untyped-def]
    with Ledger.open(data / "ledger.db") as ledger:
        book = ledger.book()
    with PriceStore.open(data / "prices.db") as store:
        prices = store.book(frozenset(h.isin for h in book.holdings()))
    return book, prices


def test_the_page_agrees_with_the_dashboard_and_the_verdict(data: Path) -> None:
    book, prices = _book_and_prices(data)
    theses = {INFY: _t(1000)}
    h = holding_page(book, prices, theses, INFY, TODAY)
    assert h is not None
    ws, _, _ = weights(book, prices)
    w = next(x for x in ws if x.isin == INFY)
    assert h.value == rupees(w.value)  # the tile's value is the page's value
    assert h.bar is not None and h.bar.weight_text == pct(tenths_of(w.bps, 10_000))
    assert h.verdict == next(v for v in for_book(book, prices, theses, TODAY) if v.isin == INFY)
    held = [x for x in book.holdings() if x.isin == INFY]
    assert len(h.accounts) == len(held)
    assert sum(Qty.parse(a.units).micros for a in h.accounts) == Qty.parse(h.units).micros
    assert holding_page(book, prices, theses, parse_isin("INE467B01029"), TODAY) is None


def test_one_holding_in_two_accounts_is_valued_across_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthetic demat with INFOSYS in two accounts (12 + 30), contributed by
    the #25 verifier: the page's value is the tile's, across both (P9)."""
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    r = runner.invoke(cli, ["import", "cas", str(FIX / "cas" / "demat-two-accounts.json")])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["prices", "import", str(FIX / "eod" / "bhavcopy-sample.csv")])
    assert r.exit_code in (0, 1), r.output
    book, prices = _book_and_prices(tmp_path)
    ws, _, _ = weights(book, prices)
    w = next(x for x in ws if x.isin == INFY)
    h = holding_page(book, prices, {}, INFY, TODAY)
    assert h is not None
    assert len(h.accounts) == 2 and h.units == "42"
    assert h.value == rupees(w.value)
    one = [a for a in h.accounts if a.units == "12"]
    assert one and one[0].value != h.value


def test_damage_inside_a_price_row_is_said_on_the_holding_page(web: TestClient, data: Path) -> None:
    import sqlite3

    c = sqlite3.connect(data / "prices.db")
    c.execute("UPDATE quotes SET on_date='2026-11-C7'")
    c.commit()
    c.close()
    r = web.get(f"/holding/{INFY}")
    assert r.status_code == 200 and "Prices could not be read" in r.text
    assert "INFOSYS" in r.text


def test_an_unpriced_holding_says_so_and_draws_no_bar(data: Path) -> None:
    book, prices = _book_and_prices(data)
    h = holding_page(book, prices, {}, HDFC, TODAY)
    assert h is not None
    assert h.value is None and h.bar is None and h.price is None
    assert h.verdict.call == "review"


def test_the_sip_months_are_this_funds_months_from_the_insight() -> None:
    import cas_fixtures as fx
    from test_insights import _book, _statement, _trade

    from stockin.insights import sip_discipline
    from stockin.prices import PriceBook

    # two SIPs in one book: the page shows only its own fund's months
    history = {
        fx.HDFC_MF: [_trade(m, 5) for m in (1, 2, 4, 5)],
        fx.ICICI_MF: [_trade(m, 10) for m in (1, 2, 3, 4, 5)],
    }
    book = _book([_statement(history, 1, 5)])
    ins = sip_discipline(book)
    assert ins is not None and ins.numbers["sips"] == 2
    isin = parse_isin(fx.HDFC_MF)
    h = holding_page(book, PriceBook(quotes={}), {}, isin, TODAY)
    assert h is not None
    own = [(p.label, p.value > 0) for p in ins.series if p.key.endswith(f":{isin}")]
    assert [(m.label, m.paid) for m in h.sip] == own
    assert len(own) == 5 and sum(not m.paid for m in h.sip) == 1
    page = _render(h)  # the page draws each month, paid or missed
    for m in h.sip:
        assert f'title="{m.label}: {"paid" if m.paid else "missed"}"' in page


def test_units_from_before_your_statements_get_no_tax_date() -> None:
    import cas_fixtures as fx
    from test_insights import _book, _sip_history, _statement

    from stockin.prices import PriceBook

    # a statement from March only: Jan and Feb units arrive as an opening balance
    book = _book([_statement(_sip_history([1, 2, 3]), 3, 3)])
    h = holding_page(book, PriceBook(quotes={}), {}, parse_isin(fx.HDFC_MF), TODAY)
    assert h is not None
    unknown = [r for r in h.lots if r.bought is None]
    known = [r for r in h.lots if r.bought is not None]
    assert len(unknown) == 1 and unknown[0].cost is None
    assert unknown[0].long_term == "date unknown" and "no known date" in h.lots_note
    assert [r.bought for r in known] == ["5 Mar 2026"]
    assert known[0].long_term == "turns long-term on 6 Mar 2027, in 107 days"
    # the tax clock bar: 366 days from purchase to long-term, 107 still to go
    assert known[0].days_left == 107
    assert known[0].held == pytest.approx((366 - 107) / 366 * 100)
    assert unknown[0].held is None and unknown[0].days_left is None


def test_lots_say_their_long_term_date_or_why_there_is_none(data: Path) -> None:
    book, prices = _book_and_prices(data)
    h = holding_page(book, prices, {}, INFY, TODAY)
    assert h is not None and h.lots
    for lot in h.lots:
        if lot.bought is None:
            assert lot.cost is None and lot.long_term == "date unknown"
            assert "no known date" in h.lots_note
        else:
            assert re.match(r"(long-term since|turns long-term on) \d", lot.long_term)
            # the bar and its label are one number: full exactly when long-term
            assert lot.held is not None and 0 <= lot.held <= 100
            assert (lot.days_left == 0) == (lot.held == 100) == lot.long_term.startswith("long")
            if lot.days_left:
                assert lot.long_term.endswith(f"in {lot.days_left} days")


@given(
    month=st.integers(1, 12),
    day=st.integers(1, 28),
    today=st.dates(date(2026, 1, 1), date(2028, 12, 31)),
)
def test_the_tax_clock_bar_matches_its_sentence_for_any_purchase_day(
    month: int, day: int, today: date
) -> None:
    import cas_fixtures as fx
    from test_insights import _book, _statement, _trade

    from stockin.prices import PriceBook

    book = _book([_statement({fx.HDFC_MF: [_trade(month, day)]}, 1, 12)])
    h = holding_page(book, PriceBook(quotes={}), {}, parse_isin(fx.HDFC_MF), today)
    assert h is not None
    for lot in h.lots:
        if lot.held is None:
            continue
        assert 0 <= lot.held <= 100
        assert (lot.days_left == 0) == (lot.held == 100) == lot.long_term.startswith("long")
        if lot.days_left:
            assert lot.long_term.endswith(f"in {lot.days_left} days")


@pytest.fixture
def web(data: Path) -> TestClient:
    return TestClient(create_app(data / "ledger.db", PORT), base_url=HOST)


def test_every_dashboard_link_opens_a_holding_page(web: TestClient, data: Path) -> None:
    with ThesisStore.open(data / "theses.db") as store:
        store.put(_t(1000))
    home = web.get("/").text
    links = set(re.findall(r'href="(/holding/[A-Z0-9]{12})"', home))
    assert links >= {f"/holding/{INFY}", f"/holding/{HDFC}"}
    assert 'class="tile' in home and f'<a href="/holding/{INFY}" class="tile' in home
    for link in links:
        r = web.get(link)
        assert r.status_code == 200, link
    page = web.get(f"/holding/{INFY}").text
    v = runner.invoke(cli, ["verdicts", "--json"]).output
    import json

    because = next(x["because"] for x in json.loads(v)["verdicts"] if x["isin"] == INFY)
    assert because.replace("'", "&#39;") in page  # page and CLI say the same
    assert "Why you hold it:" in page and f'href="/thesis/{INFY}"' in page


def test_the_pages_make_no_call_from_a_price_older_than_a_week(
    web: TestClient, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#39 verify-2: the home and the holding page judge a price's age
    against today like the CLI does. The close is from 2026-11-14: on the
    21st the 10% target gives trim everywhere, on the 22nd review, with the
    same sentence (the price, its day, what to run) on the page and the CLI."""
    import json
    from datetime import UTC, datetime

    with ThesisStore.open(data / "theses.db") as store:
        store.put(_t(1000))

    def said() -> tuple[str, str, dict[str, str]]:
        v = json.loads(runner.invoke(cli, ["verdicts", "--json"]).output)["verdicts"]
        return (
            web.get("/").text,
            web.get(f"/holding/{INFY}").text,
            next(x for x in v if x["isin"] == INFY),
        )

    home, page, cli_says = said()
    assert cli_says["call"] == "trim"
    assert "▼ trim" in home and "call-trim" in page
    monkeypatch.setattr("stockin.prices._now", lambda: datetime(2026, 11, 22, 12, tzinfo=UTC))
    home, page, cli_says = said()
    assert cli_says["call"] == "review" and "more than 7 days ago" in cli_says["because"]
    assert "₹1,500.55" in cli_says["because"] and "2026-11-14" in cli_says["because"]
    assert "▼ trim" not in home and "call-trim" not in page and "call-review" in page
    assert cli_says["because"].replace("'", "&#39;") in page


def test_a_holding_you_do_not_have_is_404_and_damage_is_said(web: TestClient, data: Path) -> None:
    assert web.get("/holding/INE000000000").status_code == 404
    (data / "prices.db").write_bytes(b"not a database at all, just text, long enough to matter")
    r = web.get(f"/holding/{INFY}")
    assert r.status_code == 200 and "Prices could not be read" in r.text


def test_the_page_draws_each_tax_clock_bar_from_its_own_lot(web: TestClient, data: Path) -> None:
    from stockin.prices import india_today

    book, prices = _book_and_prices(data)
    h = holding_page(book, prices, {}, HDFC, india_today())
    assert h is not None
    dated = [lot for lot in h.lots if lot.held is not None]
    assert dated  # the fixture's fund has purchases with dates
    page = web.get(f"/holding/{HDFC}").text
    widths = [
        float(w) for w in re.findall(r'class="clock-fill[^"]*" style="width:([\d.]+)%"', page)
    ]
    lefts = re.findall(r'<span class="clock-left">([^<]+)</span>', page)
    assert widths == [pytest.approx(lot.held, abs=0.01) for lot in dated]
    want = ["long-term" if lot.days_left == 0 else f"{lot.days_left} days" for lot in dated]
    assert lefts[: len(dated)] == want


def _render(h: object) -> str:
    from jinja2 import Environment, PackageLoader, select_autoescape

    env = Environment(loader=PackageLoader("stockin", "templates"), autoescape=select_autoescape())
    return env.get_template("holding.html").render(h=h, damaged=None, theses_damaged=None)


def _clock(page: str) -> str:
    return page.split('aria-label="Purchases and tax dates"')[1].split("<details")[0]


def test_the_tax_clock_tile_draws_every_kind_of_purchase_on_a_fixed_day() -> None:
    import cas_fixtures as fx
    from test_insights import _book, _sip_history, _statement

    from stockin.prices import PriceBook

    # March statement: Jan+Feb units arrive undated; the March buy is dated
    book = _book([_statement(_sip_history([1, 2, 3]), 3, 3)])
    isin = parse_isin(fx.HDFC_MF)
    before = holding_page(book, PriceBook(quotes={}), {}, isin, date(2026, 11, 19))
    after = holding_page(book, PriceBook(quotes={}), {}, isin, date(2027, 4, 1))
    assert before is not None and after is not None
    tile = _clock(_render(before))
    assert "107 days" in tile and "long-term</span>" not in tile
    assert "bar = time held of the year" in tile
    undated = next(r for r in before.lots if r.bought is None)
    assert re.search(r"before your statements.*?" + re.escape(f"{undated.units} units"), tile, re.S)
    # once a year has passed the bar is full and says so, never "0 days"
    tile = _clock(_render(after))
    assert re.search(r'class="clock-fill done" style="width:100.00%"', tile)
    assert "long-term</span>" in tile and "0 days" not in tile


def test_a_fund_that_is_not_equity_says_why_it_has_no_tax_bar() -> None:
    import cas_fixtures as fx
    from test_insights import _statement, _trade

    from stockin.book import Book
    from stockin.prices import PriceBook
    from stockin.sources import cas

    st_ = cas.from_casparser(_statement({fx.ICICI_MF: [_trade(1, 5), _trade(2, 5)]}, 1, 2), "sha")
    insts = tuple(i.model_copy(update={"asset_class": "debt"}) for i in st_.instruments)
    book = Book.of(tuple(st_.facts), insts, tuple(st_.accounts))
    h = holding_page(book, PriceBook(quotes={}), {}, parse_isin(fx.ICICI_MF), TODAY)
    assert h is not None
    tile = _clock(_render(h))
    assert "Not equity: different tax rules, so no long-term date is shown for 2 purchases." in tile
    assert "clock-fill" not in tile and "bar = time held" not in tile


def test_the_page_keeps_every_fact_the_view_model_holds(web: TestClient, data: Path) -> None:
    import html

    from stockin.prices import india_today

    with ThesisStore.open(data / "theses.db") as store:
        store.put(_t(1000).model_copy(update={"broken": True, "exit_if": "growth stalls"}))
    book, prices = _book_and_prices(data)
    with ThesisStore.open(data / "theses.db") as store:
        theses = store.all()
    for isin in (INFY, HDFC):
        h = holding_page(book, prices, theses, isin, india_today())
        assert h is not None
        page = html.unescape(web.get(f"/holding/{isin}").text)
        assert h.verdict.because in page
        if h.price:
            assert f"price {h.price}" in page
        if h.lots_note:
            assert h.lots_note in page
        for m in h.sip:
            assert f"{m.label}: {'paid' if m.paid else 'missed'}" in page
        table = page.split("All purchases")[1]
        for lot in h.lots:
            assert lot.units in table and lot.long_term in table
            if lot.cost:
                assert lot.cost in table


def test_a_fund_that_is_not_equity_says_why_even_beside_undated_units() -> None:
    import cas_fixtures as fx
    from test_insights import _sip_history, _statement

    from stockin.book import Book
    from stockin.prices import PriceBook
    from stockin.sources import cas

    # March statement: Jan+Feb arrive undated, March is dated; the fund is debt
    st_ = cas.from_casparser(_statement(_sip_history([1, 2, 3]), 3, 3), "sha")
    insts = tuple(i.model_copy(update={"asset_class": "debt"}) for i in st_.instruments)
    book = Book.of(tuple(st_.facts), insts, tuple(st_.accounts))
    h = holding_page(book, PriceBook(quotes={}), {}, parse_isin(fx.HDFC_MF), TODAY)
    assert h is not None and any(r.bought is None for r in h.lots)
    tile = _clock(_render(h))
    assert "Not equity: different tax rules, so no long-term date is shown for 1 purchase." in tile
    assert "before your statements" in tile
