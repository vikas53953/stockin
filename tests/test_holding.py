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
    assert h.verdict == next(v for v in for_book(book, prices, theses) if v.isin == INFY)
    held = [x for x in book.holdings() if x.isin == INFY]
    assert len(h.accounts) == len(held)
    assert sum(Qty.parse(a.units).micros for a in h.accounts) == Qty.parse(h.units).micros
    assert holding_page(book, prices, theses, parse_isin("INE467B01029"), TODAY) is None


def test_an_unpriced_holding_says_so_and_draws_no_bar(data: Path) -> None:
    book, prices = _book_and_prices(data)
    h = holding_page(book, prices, {}, HDFC, TODAY)
    assert h is not None
    assert h.value is None and h.bar is None and h.price is None
    assert h.verdict.call == "review"


def test_the_sip_months_are_this_funds_months_from_the_insight() -> None:
    import cas_fixtures as fx
    from test_insights import _book, _sip_history, _statement

    from stockin.insights import sip_discipline
    from stockin.prices import PriceBook

    book = _book([_statement(_sip_history([1, 2, 4, 5]), 1, 5)])
    ins = sip_discipline(book)
    assert ins is not None
    isin = parse_isin(fx.HDFC_MF)
    h = holding_page(book, PriceBook(quotes={}), {}, isin, TODAY)
    assert h is not None
    assert [(m.label, m.paid) for m in h.sip] == [(p.label, p.value > 0) for p in ins.series]
    assert sum(not m.paid for m in h.sip) == ins.numbers["missed"] == 1


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


def test_a_holding_you_do_not_have_is_404_and_damage_is_said(web: TestClient, data: Path) -> None:
    assert web.get("/holding/INE000000000").status_code == 404
    (data / "prices.db").write_bytes(b"not a database at all, just text, long enough to matter")
    r = web.get(f"/holding/{INFY}")
    assert r.status_code == 200 and "Prices could not be read" in r.text
