"""The Net worth screen: what it draws agrees with the numbers beside it, the
forms save through the same parser as the CLI, and a damaged assets.db or
prices.db is said on the page, never a 500."""

from __future__ import annotations

import html
import itertools
import random
import re
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from stockin.cli import app as cli
from stockin.insights import pct, tenths_of
from stockin.money import Bps, Money, Price, Qty
from stockin.networth import (
    KINDS,
    STALE_DAYS,
    AssetsDamaged,
    AssetStore,
    Bucket,
    Fd,
    Gold,
    NetWorth,
    Stated,
    close_entry,
    networth,
    parse_kind,
)
from stockin.networth_page import page, short_rupees, width
from stockin.prices import PriceBook, PriceStore
from stockin.store import Ledger
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
    assert not (tmp_path / "assets.db").exists()  # looking never creates the file


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
    assert r.text.count("Not saved") == 1  # the badge says it; the sentence says why
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


# ------------------------------------------------ verifier round 1 (PR #35)


def _two(cash: str, other: str) -> list[Stated]:
    return [
        Stated(kind="bank", label="SBI", as_of=TODAY, amount=rs(cash)),
        Stated(kind="other", label="Art", as_of=TODAY, amount=rs(other)),
    ]


@pytest.mark.parametrize(
    ("cash", "other", "drift"),
    [("2504", "7496", "+5.0 pts"), ("1996", "8004", "±0.0 pts"), ("2004", "7996", "±0.0 pts")],
)
def test_a_drift_label_is_the_shown_share_minus_the_shown_target(
    cash: str, other: str, drift: str
) -> None:
    targets: dict[Bucket, Bps] = {"cash": Bps(2000), "other": Bps(8000)}
    p = page(networth(None, PriceBook(quotes={}), _two(cash, other), targets, TODAY))
    assert {b.name: b.drift_text for b in p.bars}["Cash"] == drift


def test_the_big_number_is_net_worth_and_the_owed_share_is_of_what_you_own() -> None:
    nw = networth(None, PriceBook(quotes={}), ENTRIES, {}, TODAY)
    p = page(nw)
    assert nw.owed.paise > 0
    assert p.total == f"₹{Money(paise=nw.owned.paise - nw.owed.paise)}"
    assert p.owed_share == pct(tenths_of(nw.owed.paise, nw.owned.paise))


def test_an_old_epf_balance_is_flagged_on_its_tile() -> None:
    p = page(networth(None, PriceBook(quotes={}), ENTRIES, {}, TODAY))
    tiles = {t.label: t for g in p.groups for t in g.tiles}
    assert tiles["EPF"].pill == f"{(TODAY - date(2026, 7, 31)).days} days old"
    assert tiles["SBI savings"].pill is None


def test_the_mix_bar_draws_each_share_at_its_width(web: TestClient, data: Path) -> None:
    _seed(data)
    html = web.get("/networth").text
    drawn = re.findall(r'class="seg-part s\d" style="width:([\d.]+)%" title="([^"]+)"', html)
    assert drawn
    for w, title in drawn:
        name, share = title.rsplit(" ", 1)
        assert f"{float(w):.1f}%" == share, (name, w, share)


def test_every_close_button_on_the_page_closes_its_entry(web: TestClient, data: Path) -> None:
    _seed(data)
    html = web.get("/networth").text
    forms = re.findall(
        r'action="/networth/close"><input type="hidden" name="kind" value="([^"]+)">'
        r'<input type="hidden" name="label" value="([^"]+)">',
        html,
    )
    assert len(forms) == len(ENTRIES)
    assert {k for k, _ in forms} <= set(KINDS)  # the stored kind, never its display name
    for kind, label in forms:
        r = web.post("/networth/close", data={"kind": kind, "label": label}, follow_redirects=False)
        assert r.status_code == 303, (kind, label, r.text[:300])
    assert "I no longer hold this" not in web.get("/networth").text


def test_a_refused_target_mix_keeps_what_you_typed(web: TestClient, data: Path) -> None:
    _seed(data)  # a saved mix exists: equity 60, debt 40
    typed = {"t_equity": "60", "t_debt": "20", "t_gold": "10", "t_cash": "9"}
    r = web.post("/networth/targets", data=typed)
    assert r.status_code == 400 and "add to 99.0%" in r.text
    for name, v in typed.items():
        assert f'name="{name}" inputmode="decimal" value="{v}"' in r.text, name
    assert 'name="t_other" inputmode="decimal" value=""' in r.text
    assert r.text.count("Not saved") == 1
    assert AssetStore.open(data / "assets.db").targets() == {"equity": 6000, "debt": 4000}


@pytest.mark.parametrize("kind", ["bank", "boat"])
def test_a_refused_close_says_not_saved_once(web: TestClient, kind: str) -> None:
    r = web.post("/networth/close", data={"kind": kind, "label": "nope"})
    assert r.status_code == 400 and r.text.count("Not saved") == 1


def test_a_refused_fd_keeps_its_kind(web: TestClient) -> None:
    r = web.post("/networth/entry", data={"kind": "fd", "label": "HDFC FD", "principal": "x"})
    assert r.status_code == 400 and '<option value="fd" selected>' in r.text


def test_damage_hides_every_close_button(web: TestClient, data: Path) -> None:
    _seed(data)
    with sqlite3.connect(data / "assets.db") as c:
        c.execute("UPDATE entries SET body = 'junk' WHERE kind = 'bank'")
    c.close()
    html = web.get("/networth").text
    assert "Your entries could not be read" in html and "/networth/close" not in html


def test_looking_at_the_page_never_writes_assets(web: TestClient, data: Path) -> None:
    assert web.get("/networth").status_code == 200
    assert not (data / "assets.db").exists()
    (data / "assets.db").write_bytes(b"")
    assert web.get("/networth").status_code == 200
    assert (data / "assets.db").read_bytes() == b""


# ------------------------------------------------ verifier round 2 (PR #35)


@pytest.fixture
def bare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A data folder with no ledger and no prices: only what the owner typed."""
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    monkeypatch.setattr("stockin.web.india_today", lambda: TODAY)
    return TestClient(create_app(tmp_path / "ledger.db", PORT), base_url=HOST)


def _engine(folder: Path) -> NetWorth:
    """The net worth the engine works out from the files in `folder`, read
    without going through the page."""
    book, prices = None, PriceBook(quotes={})
    if (folder / "ledger.db").exists():
        with Ledger.open(folder / "ledger.db") as ledger:
            book = ledger.book()
        with PriceStore.open(folder / "prices.db") as ps:
            prices = ps.book(frozenset(h.isin for h in book.holdings()))
    with AssetStore.open(folder / "assets.db") as store:
        entries, targets = store.entries(), store.targets()
    return networth(book, prices, entries, targets, TODAY)


def _hero(page_: str) -> str:
    got = re.findall(r'<h1 class="nw-total[^"]*">([^<]*)</h1>', page_)
    assert len(got) == 1, got
    return html.unescape(got[0])


def _notice(page_: str) -> str:
    """The whole refusal notice as the owner reads it: tags out, spaces folded."""
    got = re.findall(r'<div class="notice bad" role="alert" id="error">(.*?)</div>', page_, re.S)
    assert len(got) == 1, got
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", got[0])).split())


def test_the_hero_number_on_the_page_is_the_engines_net_worth(web: TestClient, data: Path) -> None:
    _seed(data)  # market holdings, own entries and a loan
    page_ = web.get("/networth").text
    nw = _engine(data)
    assert nw.owed.paise > 0 and nw.total != nw.owned  # a loan: the two numbers differ
    assert _hero(page_) == f"₹{nw.total}"
    assert f"</i>own ₹{nw.owned}</span>" in page_
    assert f"</i>owe ₹{nw.owed} (" in page_


def test_the_page_names_the_latest_price_day(web: TestClient, data: Path, tmp_path: Path) -> None:
    older = tmp_path / "NAVAll-older.txt"
    older.write_text(
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;"
        "Net Asset Value;Date\n \nOpen Ended Schemes(Equity Scheme - Flexi Cap Fund)\n \n"
        "HDFC Mutual Fund\n \n"
        "118955;INF179K01BE2;-;HDFC Flexi Cap Fund;Direct Plan;Growth Option;2100.1234;"
        "10-Nov-2026\n"
    )
    r = runner.invoke(cli, ["prices", "import", str(older)])
    assert r.exit_code == 0, r.output
    with Ledger.open(data / "ledger.db") as ledger:
        held = frozenset(h.isin for h in ledger.book().holdings())
    with PriceStore.open(data / "prices.db") as ps:
        days = sorted({q.on for q in ps.book(held).quotes.values()})
    assert len(days) == 2, days  # the fund is priced on an older day than the share
    label = re.findall(
        r'<span class="stat-label">Net worth · ([^<]+)</span>', web.get("/networth").text
    )
    assert label == [f"{TODAY.isoformat()} · prices {days[-1].isoformat()}"]


@pytest.mark.parametrize(
    ("cash", "other", "target", "drift"),
    [
        ("2004", "7996", 1996, "±0.0 pts"),  # 20.0% against 20.0%; the raw gap rounds to +0.1
        ("3338", "6662", 3333, "+0.1 pts"),  # 33.4% against 33.3%; the raw gap rounds to 0.0
    ],
)
def test_a_drift_label_follows_the_shown_numbers_when_the_raw_gap_says_otherwise(
    bare: TestClient, tmp_path: Path, cash: str, other: str, target: int, drift: str
) -> None:
    targets: dict[Bucket, Bps] = {"cash": Bps(target), "other": Bps(10_000 - target)}
    with AssetStore.open(tmp_path / "assets.db") as store:
        for e in _two(cash, other):
            store.put(e)
        store.set_targets(targets)
    nw = _engine(tmp_path)
    s = {x.bucket: x for x in nw.slices}["cash"]
    assert s.target == target and s.drift is not None
    shown = tenths_of(s.bps, 10_000) - tenths_of(target, 10_000)
    assert tenths_of(s.drift, 10_000) != shown  # this case tells the two apart
    sign = "+" if shown > 0 else ("-" if shown < 0 else "±")
    assert drift == f"{sign}{abs(shown) // 10}.{abs(shown) % 10} pts"
    bar = {b.name: b for b in page(nw).bars}["Cash"]
    assert bar.drift_text == drift
    assert (bar.share, bar.target_text) == (
        pct(tenths_of(s.bps, 10_000)),
        pct(tenths_of(target, 10_000)),
    )
    drawn = re.findall(
        r'<span>Cash</span><span class="drift \w+">([^<]+)</span>', bare.get("/networth").text
    )
    assert [html.unescape(d) for d in drawn] == [drift]


@pytest.mark.parametrize("kind", ["bank", "boat"])
def test_a_refused_close_notice_is_the_badge_and_then_only_the_reason(
    web: TestClient, kind: str
) -> None:
    with pytest.raises(ValueError) as why:
        close_entry((), parse_kind(kind), "nope", TODAY)
    r = web.post("/networth/close", data={"kind": kind, "label": "nope"})
    assert r.status_code == 400
    assert _notice(r.text) == f"✕ Not saved {why.value}"


def test_a_refused_target_mix_notice_is_the_badge_and_then_only_the_reason(
    web: TestClient,
) -> None:
    r = web.post("/networth/targets", data={"t_equity": "60", "t_debt": "39"})
    assert r.status_code == 400
    assert _notice(r.text) == "✕ Not saved Target mix: the targets add to 99.0%, not 100%"


def test_the_stale_flag_starts_the_day_after_ninety_days(bare: TestClient, tmp_path: Path) -> None:
    at, past = STALE_DAYS, STALE_DAYS + 1
    with AssetStore.open(tmp_path / "assets.db") as store:
        for label, days in (("At the line", at), ("Past the line", past)):
            store.put(
                Stated(
                    kind="bank",
                    label=label,
                    as_of=TODAY - timedelta(days=days),
                    amount=rs("1000"),
                )
            )
    nw = _engine(tmp_path)
    tiles = {t.label: t for g in page(nw).groups for t in g.tiles}
    assert tiles["At the line"].pill is None
    assert tiles["Past the line"].pill == f"{past} days old"
    page_ = bare.get("/networth").text
    assert f"{past} days old" in page_ and f"{at} days old" not in page_
    # the engine's own note draws the line at the same day
    notes = " ".join(n.text for n in nw.notes)
    assert f"Past the line was last updated {past} days ago" in notes
    assert "At the line was last updated" not in notes


def test_a_group_total_adds_every_line_in_the_group(bare: TestClient, tmp_path: Path) -> None:
    with AssetStore.open(tmp_path / "assets.db") as store:
        for label, amount in (("SBI savings", "250000"), ("HDFC salary", "40000.50")):
            store.put(Stated(kind="bank", label=label, as_of=TODAY, amount=rs(amount)))
        for label, amount in (("Home loan", "300000"), ("Car loan", "125000.25")):
            store.put(Stated(kind="loan", label=label, as_of=TODAY, amount=rs(amount)))
    nw = _engine(tmp_path)
    cash = {s.bucket: s for s in nw.slices}["cash"].value
    assert cash == rs("290000.50") and nw.owed == rs("425000.25")
    assert {g.name: g.total for g in page(nw).groups} == {
        "Cash": f"₹{cash}",
        "Loans": f"₹{nw.owed}",
    }
    hold = bare.get("/networth").text.split('aria-label="What you hold"')[1].split("</section>")[0]
    drawn = re.findall(r"</i>([A-Za-z ]+) (₹[\d,.]+)</span>", hold)
    assert drawn == [("Cash", f"₹{cash}"), ("Loans", f"₹{nw.owed}")]


def test_a_saved_target_with_hundredths_comes_back_whole_in_the_form(
    bare: TestClient, tmp_path: Path
) -> None:
    saved: dict[Bucket, Bps] = {"equity": Bps(3333), "debt": Bps(3333), "cash": Bps(3334)}
    with AssetStore.open(tmp_path / "assets.db") as store:
        store.set_targets(saved)
    page_ = bare.get("/networth").text
    boxes = dict(re.findall(r'name="(t_\w+)" inputmode="decimal" value="([^"]*)"', page_))
    assert boxes == {
        "t_equity": "33.33",
        "t_debt": "33.33",
        "t_gold": "",
        "t_cash": "33.34",
        "t_retirement": "",
        "t_real_estate": "",
        "t_other": "",
    }
    # saving the form as it was filled in changes nothing
    r = bare.post("/networth/targets", data=boxes, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    with AssetStore.open(tmp_path / "assets.db") as store:
        assert store.targets() == saved


_KILLED_MID_WRITE = """
import os, sys
from pathlib import Path
from stockin.networth import AssetStore
conn = AssetStore.open(Path(sys.argv[1]))._conn
conn.isolation_level = None
conn.execute("PRAGMA cache_size=1")
conn.execute("BEGIN")
conn.execute("DELETE FROM targets")
for i in range(200):  # more than the cache holds, so changed pages reach the file
    conn.execute(
        "INSERT INTO entries VALUES ('other', ?, '2026-11-20', 0, ?)", (f"junk{i}", "x" * 900)
    )
os._exit(9)  # killed: no commit, no rollback
"""


def test_a_write_killed_mid_commit_is_rolled_back_by_opening_the_page(
    web: TestClient, data: Path
) -> None:
    """A real hot journal: a child process starts a big write through the
    real store and dies before it commits. A read-only open cannot roll that
    back; the page must, the way any CLI command does."""
    _seed(data)
    db, journal = data / "assets.db", data / "assets.db-journal"
    saved = db.read_bytes()
    before = _engine(data)
    r = subprocess.run([sys.executable, "-c", _KILLED_MID_WRITE, str(db)], check=False)
    assert r.returncode == 9
    assert journal.stat().st_size > 0 and db.read_bytes() != saved  # half a write is in the file
    with pytest.raises(AssetsDamaged, match="readonly"):
        AssetStore.open(db, readonly=True)
    assert journal.exists() and db.read_bytes() != saved  # still as the crash left it

    r = web.get("/networth")
    assert r.status_code == 200 and "could not be read" not in r.text
    assert _hero(r.text) == f"₹{before.total}" and before.owned.paise > 0
    for e in ENTRIES:
        assert e.label in r.text
    assert "junk" not in r.text
    assert 'action="/networth/entry"' in r.text and 'action="/networth/targets"' in r.text
    assert r.text.count('action="/networth/close"') == len(ENTRIES)
    assert db.read_bytes() == saved and not journal.exists()  # back to the last saved state
    assert _engine(data) == before


def test_a_leftover_journal_does_not_hide_real_damage(web: TestClient, data: Path) -> None:
    _seed(data)
    with sqlite3.connect(data / "assets.db") as c:
        c.execute("UPDATE entries SET body = 'junk' WHERE kind = 'bank'")
    c.close()
    (data / "assets.db-journal").write_bytes(b"left behind, and not a journal at all")
    r = web.get("/networth")
    assert r.status_code == 200 and "Your entries could not be read" in r.text
    assert "/networth/close" not in r.text and 'action="/networth/entry"' not in r.text


def test_viewing_opens_assets_for_writing_only_to_roll_back_a_journal(
    web: TestClient, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(data)
    opened: list[bool] = []
    real = AssetStore.open

    def spy(path: Path, *, readonly: bool = False) -> AssetStore:
        opened.append(readonly)
        return real(path, readonly=readonly)

    monkeypatch.setattr(AssetStore, "open", staticmethod(spy))
    assert "SBI savings" in web.get("/networth").text
    assert opened == [True]  # a healthy file: read-only, once
    with sqlite3.connect(data / "assets.db") as c:
        c.execute("UPDATE entries SET body = 'junk' WHERE kind = 'bank'")
    c.close()
    opened.clear()
    assert "Your entries could not be read" in web.get("/networth").text
    assert opened == [True]  # damage with no journal beside it: still read-only, once


def test_a_damaged_empty_page_does_not_point_at_a_form_it_hides(
    bare: TestClient, tmp_path: Path
) -> None:
    healthy = bare.get("/networth").text
    assert "with the form below" in healthy and 'action="/networth/entry"' in healthy
    junk = b"not a database, just some words long enough"
    (tmp_path / "assets.db").write_bytes(junk)
    empty = bare.get("/networth").text  # nothing readable, no market holdings: the empty card
    assert "Your entries could not be read" in empty and "Start your net worth" in empty
    assert "<form" not in empty and "below" not in empty
    assert "Nothing can be added here until assets.db can be read again." in empty
    assert "Import a CAS" in empty  # still true: importing does not need assets.db
    assert (tmp_path / "assets.db").read_bytes() == junk


def test_a_damaged_page_with_market_holdings_does_not_say_set_a_target_below(
    web: TestClient, data: Path
) -> None:
    healthy = web.get("/networth").text  # market holdings, no target mix yet
    assert "no target mix yet: set one below" in healthy
    assert 'action="/networth/targets"' in healthy
    (data / "assets.db").write_bytes(b"not a database, just some words long enough")
    damaged = web.get("/networth").text
    assert "Your entries could not be read" in damaged and "Against your target" in damaged
    assert "<form" not in damaged and "below" not in damaged
    assert "your target mix could not be read" in damaged
