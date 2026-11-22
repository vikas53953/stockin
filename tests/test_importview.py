"""The Import and Reconcile pages: statement coverage as a month grid,
reconcile as counts and twin bars, each label built from the same numbers
as its bar, and the pages themselves through HTTP."""

from __future__ import annotations

import calendar
import random
import re
from datetime import date, timedelta
from pathlib import Path

import cas_fixtures as fx
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from stockin.book import Check
from stockin.facts import AccountId, parse_isin
from stockin.importview import MAX_MONTHS, coverage, source_label, tally, twin
from stockin.money import Qty
from stockin.store import Period
from stockin.web import create_app

SOURCES = ("cams_kfin_cas", "nsdl_cas", "cdsl_cas")
PORT = 8765
HOST = f"http://127.0.0.1:{PORT}"


@st.composite
def periods(draw: st.DrawFn) -> list[Period]:
    out = []
    for _ in range(draw(st.integers(1, 6))):
        start = draw(st.dates(date(2025, 1, 1), date(2026, 12, 31)))
        end = start + timedelta(days=draw(st.integers(0, 120)))
        out.append(Period(source=draw(st.sampled_from(SOURCES)), start=start, end=end))
    return out


def _months(a: date, b: date) -> set[tuple[int, int]]:
    out, y, m = set(), a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.add((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


@given(periods(), st.randoms(use_true_random=False))
def test_each_square_is_covered_exactly_when_a_statement_includes_that_month(
    ps: list[Period], rnd: random.Random
) -> None:
    cover = coverage(tuple(ps))
    assert cover is not None
    shuffled = list(ps)
    rnd.shuffle(shuffled)
    assert coverage(tuple(shuffled)) == cover  # import order never changes the picture
    assert cover.statements == len(ps) and cover.headline.startswith(f"{len(ps)} statement")
    assert len(cover.months) <= MAX_MONTHS
    names = {
        f"{m} {y}": (y, i + 1)
        for y in range(2024, 2028)
        for i, m in enumerate(calendar.month_abbr[1:])
    }
    shown = [names[m] for m in cover.months]
    assert shown == sorted(shown)
    assert cover.months[-1] in cover.headline
    for src in {p.source for p in ps}:
        lane = next(lane for lane in cover.lanes if lane.label == source_label(src))
        have = set().union(*(_months(p.start, p.end) for p in ps if p.source == src))
        assert list(lane.covered) == [m in have for m in shown]
        gaps = [m for m in shown if min(have) <= m <= max(have) and m not in have]
        assert [names[g] for g in lane.gaps] == gaps
        line = [g for g in cover.gap_lines if g.startswith(f"No {lane.label} ")]
        assert (line == [f"No {lane.label} statement for {', '.join(lane.gaps)}."]) == bool(gaps)
        assert bool(line) == bool(gaps)


def test_no_statements_draws_no_grid() -> None:
    assert coverage(()) is None


statuses = st.sampled_from(("match", "inferred", "mismatch"))


@given(st.lists(statuses, min_size=1, max_size=40))
def test_the_reconcile_bar_counts_every_check_once(ss: list[str]) -> None:
    isin = parse_isin(fx.HDFC_MF)
    checks = tuple(
        Check(
            kind="balance",
            account=AccountId("a"),
            isin=isin,
            on=date(2026, 1, 31),
            stated=Qty(micros=1),
            computed=Qty(micros=1),
            status=s,  # type: ignore[arg-type]
        )
        for s in ss
    )
    t = tally(checks)
    assert [x.status for x in t] == [s for s in ("mismatch", "inferred", "match") if s in ss]
    assert {x.status: x.count for x in t} == {s: ss.count(s) for s in set(ss)}
    assert abs(sum(x.share for x in t) - 100) < 1e-9


@given(st.integers(0, 10**15), st.integers(0, 10**15))
def test_twin_bars_put_stated_and_trades_on_one_scale(a: int, b: int) -> None:
    w = twin(Qty(micros=a), Qty(micros=b))
    if a == b == 0:
        assert (w.stated, w.computed) == (0, 0)
        return
    assert max(w.stated, w.computed) == 100
    assert abs(w.stated - a / max(a, b) * 100) < 1e-9
    assert abs(w.computed - b / max(a, b) * 100) < 1e-9
    if a and b:
        assert abs(w.stated / w.computed - a / b) < 1e-9 * max(1, a / b)


# ------------------------------------------------------------------ pages


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "data" / "ledger.db", PORT), base_url=HOST)


def _upload(client: TestClient, path: Path) -> str:
    with path.open("rb") as fh:
        r = client.post("/import", files={"statement": (path.name, fh, "application/json")})
    assert r.status_code == 200
    return r.text


def test_the_import_page_shows_what_each_source_covers(tmp_path: Path) -> None:
    client = _client(tmp_path)
    page = client.get("/import").text
    assert "No statements yet" in page and 'type="file"' in page and 'class="cover"' not in page
    files = fx.write_all(tmp_path / "in")
    result = _upload(client, files[0])
    assert "5 new facts" in result and 'class="cover"' in result
    for f in files[1:]:
        _upload(client, f)
    page = client.get("/import").text
    from stockin.store import Ledger

    with Ledger.open(tmp_path / "data" / "ledger.db") as ledger:
        cover = coverage(ledger.periods())
    assert cover is not None and cover.headline in page
    squares = re.findall(r'<i class="cover-m( on)?"', page)
    assert [bool(s) for s in squares] == [c for lane in cover.lanes for c in lane.covered]
    for line in cover.gap_lines:
        assert line in page


def test_a_mismatch_is_drawn_as_stated_against_trades(tmp_path: Path) -> None:
    client = _client(tmp_path)
    jan = fx.rta(
        "01-Jan-2026",
        "31-Jan-2026",
        [
            fx.folio(
                "1/1",
                "Test AMC",
                [
                    fx.scheme(
                        fx.HDFC_MF,
                        "Test Fund",
                        "0",
                        "10",
                        [fx.txn("2026-01-05", "PURCHASE", "10", "1000")],
                    )
                ],
            )
        ],
    )
    feb = fx.rta(
        "01-Feb-2026",
        "28-Feb-2026",
        [fx.folio("1/1", "Test AMC", [fx.scheme(fx.HDFC_MF, "Test Fund", "7", "7", [])])],
    )
    _upload(client, fx.write(jan, tmp_path / "jan.json"))
    _upload(client, fx.write(feb, tmp_path / "feb.json"))
    page = client.get("/reconcile").text
    assert "mismatch" in page and "Where they disagree" in page
    bad = page.split("Where they disagree")[1].split("</section>")[0]
    stated = [float(w) for w in re.findall(r'class="twin-stated" style="width:([\d.]+)%"', bad)]
    trades = [float(w) for w in re.findall(r'class="twin-computed" style="width:([\d.]+)%"', bad)]
    nums = re.findall(r'<span class="num">([\d.]+)</span>', bad)
    assert len(stated) == len(trades) == len(nums) // 2 >= 1
    for i, (s, t) in enumerate(zip(stated, trades, strict=True)):
        a, b = float(nums[2 * i]), float(nums[2 * i + 1])
        assert max(s, t) == 100 and abs(s / 100 * max(a, b) - a) < 1e-6 * max(a, b)


def test_reconcile_with_no_statements_points_to_import(tmp_path: Path) -> None:
    page = _client(tmp_path).get("/reconcile").text
    assert "No statements yet" in page and 'href="/import"' in page


def test_a_missing_month_is_said_under_the_grid(tmp_path: Path) -> None:
    client = _client(tmp_path)

    def month(first: str, last: str, units: str) -> fx.CASData:
        scheme = fx.scheme(fx.HDFC_MF, "Test Fund", units, units, [])
        return fx.rta(first, last, [fx.folio("1/1", "Test AMC", [scheme])])

    _upload(client, fx.write(month("01-Jan-2026", "31-Jan-2026", "5"), tmp_path / "jan.json"))
    _upload(client, fx.write(month("01-Mar-2026", "31-Mar-2026", "5"), tmp_path / "mar.json"))
    page = client.get("/import").text
    line = "No Mutual funds (CAMS / KFintech) statement for Feb 2026."
    said = re.findall(r'<p class="small">.*?</p>', page, re.S)
    assert sum(line in p for p in said) == 1
    assert re.findall(r'<i class="cover-m( on)?"', page) == [" on", "", " on"]
