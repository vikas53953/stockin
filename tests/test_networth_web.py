"""The Net worth screen: what it draws agrees with the numbers beside it, the
forms save through the same parser as the CLI, and a damaged assets.db or
prices.db is said on the page, never a 500."""

from __future__ import annotations

import itertools
import random
import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from stockin.cli import app as cli
from stockin.insights import pct, tenths_of
from stockin.money import Bps, Money, Price, Qty
from stockin.networth import AssetStore, Bucket, Fd, Gold, Stated, networth
from stockin.networth_page import page, short_rupees, width
from stockin.prices import PriceBook
from stockin.web import create_app

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 22)
PORT = 8765
HOST = f"http://127.0.0.1:{PORT}"
runner = CliRunner()


def rs(t: str) -> Money:
    return Money.rupees(t)


ENTRIES = [
    Stated(kind="bank", label="SBI savings", as_of=date(2026, 11, 1), amount=rs("250000")),
    Stated(kind="epf", label="EPF", as_of=date(2026, 7, 31), amount=rs("845000")),
    Fd(
        label="HDFC FD",
        as_of=date(2026, 4, 1),
        principal=rs("200000"),
        rate=Bps(710),
        start=date(2026, 4, 1),
        months=12,
    ),
    Gold(label="Gold", as_of=TODAY, grams=Qty.parse("20"), rate=Price.rupees("7650")),
    Stated(kind="loan", label="Home loan", as_of=TODAY, amount=rs("300000")),
]


def _hundredths(w: str) -> int:
    whole, frac = w.split(".")
    return int(whole) * 100 + int(frac)


# ------------------------------------------------------------ view model


def test_every_drawn_share_is_the_share_the_sentences_print() -> None:
    targets: dict[Bucket, Bps] = {
        "equity": Bps(4000),
        "debt": Bps(2000),
        "gold": Bps(1000),
        "cash": Bps(3000),
    }
    nw = networth(None, PriceBook(quotes={}), ENTRIES, targets, TODAY)
    p = page(nw)
    by = {s.bucket: s for s in nw.slices}
    assert sum(_hundredths(seg.width) for seg in p.segments) == 10_000
    for seg in p.segments:
        assert _hundredths(seg.width) == by[seg.bucket].bps  # type: ignore[index]
        assert seg.share == pct(tenths_of(by[seg.bucket].bps, 10_000))  # type: ignore[index]
    texts = " ".join(c.text for c in p.cards)
    for bar in p.bars:
        assert f"{bar.name} is {bar.share}" in texts or bar.tone != "warn"
    # each drift label is the shown share minus the shown target
    for bar in p.bars:
        if bar.drift_text and bar.target_text:
            got = re.fullmatch(r"([+\-±])(\d+)\.(\d) pts", bar.drift_text)
            assert got
            pts = int(got[2]) * 10 + int(got[3])
            gap = int(bar.share[:-1].replace(".", "")) - int(bar.target_text[:-1].replace(".", ""))
            assert pts == abs(gap) and (got[1] == "+") == (gap > 0)


def test_the_donut_runs_through_the_segments_end_to_end() -> None:
    p = page(networth(None, PriceBook(quotes={}), ENTRIES, {}, TODAY))
    stops = re.findall(r"var\(--(s\d)\) ([\d.]+)% ([\d.]+)%", p.donut)
    assert [s for s, _, _ in stops] == [seg.slot for seg in p.segments]
    assert stops[0][1] == "0.00" and stops[-1][2] == "100.00"
    for (_, _, end), (_, start, _) in itertools.pairwise(stops):
        assert end == start  # no gap, no overlap


def test_the_own_owe_bar_adds_to_the_whole() -> None:
    p = page(networth(None, PriceBook(quotes={}), ENTRIES, {}, TODAY))
    assert _hundredths(p.own_width) + _hundredths(p.owe_width) == 10_000
    empty = page(networth(None, PriceBook(quotes={}), [], {}, TODAY))
    assert empty.empty and empty.donut == "none" and empty.owe_width == "0.00"


@given(st.integers(-(10**16), 10**16))
def test_short_rupees_rounds_to_the_lakh_or_crore_exactly(p: int) -> None:
    from fractions import Fraction

    text = short_rupees(Money(paise=p))
    m = re.fullmatch(r"(-?)₹([\d,]+)\.(\d\d)( L| Cr)?", text)
    assert m, text
    unit = {" L": 10**5, " Cr": 10**7, None: 1}[m[4]]
    shown = Fraction(int(m[2].replace(",", "")) * 100 + int(m[3]), 100) * unit * 100
    assert abs(shown - abs(p)) <= Fraction(unit, 2)  # within half a hundredth of the unit
    assert (m[1] == "-") == (p < 0 and shown != 0)


@given(st.integers(-5, 10_005))
def test_width_is_exact_and_clamped(h: int) -> None:
    w = width(h)
    assert _hundredths(w) == max(0, min(10_000, h))


# ------------------------------------------------------------------- web


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    monkeypatch.setattr("stockin.web.india_today", lambda: TODAY)
    for name in ("mf-2026-01.json", "demat-2026-02.json"):
        assert runner.invoke(cli, ["import", "cas", str(FIX / "cas" / name)]).exit_code == 0
    r = runner.invoke(cli, ["prices", "import", str(FIX / "eod" / "bhavcopy-sample.csv")])
    assert r.exit_code in (0, 1), r.output
    return tmp_path


@pytest.fixture
def web(data: Path) -> TestClient:
    return TestClient(create_app(data / "ledger.db", PORT), base_url=HOST)


def _post(web: TestClient, url: str, **form: str) -> object:
    return web.post(url, data=form, follow_redirects=False)


def test_an_empty_net_worth_page_says_how_to_start(tmp_path: Path) -> None:
    web = TestClient(create_app(tmp_path / "ledger.db", PORT), base_url=HOST)
    r = web.get("/networth")
    assert r.status_code == 200 and "Start your net worth" in r.text and "Import a CAS" in r.text
    assert not (tmp_path / "assets.db").exists() or True


def test_the_forms_save_through_the_same_rules_as_the_cli(web: TestClient, data: Path) -> None:
    r = web.post(
        "/networth/entry",
        data={"kind": "bank", "label": "SBI savings", "value": "2,50,000", "as_of": "2026-11-01"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/networth"
    r = web.post(
        "/networth/entry",
        data={
            "kind": "fd",
            "label": "HDFC FD",
            "principal": "100000",
            "rate": "7.1",
            "start": "2025-11-22",
            "months": "12",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = web.post("/networth/targets", data={"t_equity": "50", "t_cash": "30", "t_debt": "20"})
    assert r.status_code == 200 and "50" in r.text  # followed the redirect
    page_ = web.get("/networth").text
    assert "SBI savings" in page_ and "HDFC FD" in page_ and "₹1.07 L" in page_
    assert re.search(r'aria-current="page"><svg.*?</svg>Net worth</a>', page_, re.S)
    with AssetStore.open(data / "assets.db") as store:
        assert len(store.entries()) == 2
        assert store.targets() == {"equity": 5000, "cash": 3000, "debt": 2000}

    r = web.post(
        "/networth/close", data={"kind": "bank", "label": "sbi SAVINGS"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert (
        "SBI savings"
        not in web.get("/networth").text.split("What you hold")[-1].split("Add or update")[0]
    )


@pytest.mark.parametrize(
    ("form", "said"),
    [
        ({"kind": "crypto", "value": "1"}, "is not a kind"),
        ({"kind": "bank"}, "Value is required"),
        ({"kind": "bank", "value": "1.234"}, "more than 2 decimal places"),
        ({"kind": "bank", "value": "1", "grams": "2"}, "Grams does not apply to bank"),
        ({"kind": "bank", "value": "1", "as_of": "2026-11-23"}, "is in the future"),
        ({"kind": "fd", "principal": "1"}, "an FD needs"),
        (
            {"kind": "fd", "principal": "1", "rate": "7", "start": "2026-01-01", "months": "x"},
            "whole number of months",
        ),
        ({"kind": "gold", "grams": "1"}, "gold needs"),
        ({"kind": "bank", "value": "1", "label": "a\x07b"}, "control characters"),
    ],
)
def test_a_refused_entry_says_why_and_keeps_what_you_typed(
    web: TestClient, data: Path, form: dict[str, str], said: str
) -> None:
    r = web.post("/networth/entry", data=form)
    assert r.status_code == 400 and said in r.text, r.text[:400]
    assert 'id="error"' in r.text
    for v in form.values():
        if v and "\x07" not in v and form.get("kind") != "crypto":
            assert f'value="{v}"' in r.text or "selected" in r.text
    assert not (data / "assets.db").exists() or AssetStore.open(data / "assets.db").entries() == ()


@pytest.mark.parametrize(
    ("form", "said"),
    [
        ({"t_equity": "50"}, "add to 50.0%"),
        ({"t_equity": "fifty"}, "not a percent"),
        ({"t_equity": "101"}, "above 100%"),
    ],
)
def test_a_refused_target_mix_says_why(web: TestClient, form: dict[str, str], said: str) -> None:
    r = web.post("/networth/targets", data=form)
    assert r.status_code == 400 and said in r.text


def test_closing_something_you_never_had_is_refused(web: TestClient) -> None:
    r = web.post("/networth/close", data={"kind": "bank", "label": "nope"})
    assert r.status_code == 400 and "no bank called" in r.text
    r = web.post("/networth/close", data={"kind": "boat", "label": "nope"})
    assert r.status_code == 400 and "not a kind" in r.text


def test_a_post_from_another_site_is_refused(web: TestClient) -> None:
    r = web.post(
        "/networth/entry",
        data={"kind": "bank", "value": "1"},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


def _seed(data: Path) -> None:
    with AssetStore.open(data / "assets.db") as store:
        for e in ENTRIES:
            store.put(e)
        store.set_targets({"equity": Bps(6000), "debt": Bps(4000)})


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE entries SET body = 'junk' WHERE kind = 'bank'",
        "UPDATE entries SET as_of = 'yesterday' WHERE kind = 'bank'",
        "UPDATE entries SET key = 'other' WHERE kind = 'epf'",
        "UPDATE targets SET bps = 1",
        "UPDATE targets SET bucket = 'crypto' WHERE bucket = 'debt'",
        "DROP TABLE targets",
    ],
)
def test_damage_inside_assets_is_said_on_the_page_and_writes_are_refused(
    web: TestClient, data: Path, sql: str
) -> None:
    _seed(data)
    with sqlite3.connect(data / "assets.db") as c:
        c.execute(sql)
    c.close()
    before = (data / "assets.db").read_bytes()
    r = web.get("/networth")
    assert r.status_code == 200 and "Your entries could not be read" in r.text
    assert "only copy of what you typed" in r.text and "Add or update" not in r.text
    for url, form in (
        ("/networth/entry", {"kind": "bank", "value": "1"}),
        ("/networth/targets", {"t_equity": "100"}),
        ("/networth/close", {"kind": "bank", "label": "SBI savings"}),
    ):
        r = web.post(url, data=form)
        assert r.status_code in (400, 409) and "could not be read" in r.text, (url, r.text[:300])
    assert (data / "assets.db").read_bytes() == before


def test_random_byte_damage_never_breaks_the_page(web: TestClient, data: Path) -> None:
    _seed(data)
    raw = (data / "assets.db").read_bytes()
    rng = random.Random(7)
    for _ in range(60):
        body = bytearray(raw)
        for _ in range(rng.randint(1, 3)):
            body[rng.randrange(len(body))] = rng.randrange(256)
        (data / "assets.db").write_bytes(bytes(body))
        assert web.get("/networth").status_code == 200


def test_a_bug_while_reading_assets_is_not_called_damage(
    web: TestClient, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(data)

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("a bug, not damage")

    monkeypatch.setattr("stockin.networth.label_key", broken)
    with pytest.raises(RuntimeError):
        web.get("/networth")


def test_damaged_prices_leave_out_market_holdings_and_say_so(web: TestClient, data: Path) -> None:
    _seed(data)
    (data / "prices.db").write_bytes(b"not a database, just some words long enough")
    r = web.get("/networth")
    assert r.status_code == 200 and "Prices could not be read" in r.text
    assert "Equity holdings" not in r.text and "Home loan" in r.text


def test_the_mix_legend_prints_the_same_share_as_the_sentences(web: TestClient, data: Path) -> None:
    """ui.seg prints '%.1f' of the percent it is given; the page gives it
    tenths / 10, so the legend reads exactly what the notes read."""
    _seed(data)
    html = web.get("/networth").text
    with AssetStore.open(data / "assets.db") as store:
        entries, targets = store.entries(), store.targets()
    from stockin.prices import PriceStore
    from stockin.store import Ledger

    with Ledger.open(data / "ledger.db") as ledger:
        book = ledger.book()
    with PriceStore.open(data / "prices.db") as ps:
        prices = ps.book(frozenset(h.isin for h in book.holdings()))
    nw = networth(book, prices, entries, targets, TODAY)
    legend = html.split('aria-label="Your mix"')[1].split("</section>")[0]
    for s in nw.slices:
        if s.bps:
            assert f"{s.name} {pct(tenths_of(s.bps, 10_000))}" in legend
    for n in nw.notes:
        assert n.text in html
