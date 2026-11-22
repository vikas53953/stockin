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
import pytest
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


# ---------------------------------------------- damage never hides a result

ROW_DAMAGE = {
    "bad-end": "UPDATE statements SET period_end='2026-13-45'",
    "bad-start": "UPDATE statements SET period_start='junk'",
    "null-ish": "UPDATE statements SET period_end=''",
    "blob": "UPDATE statements SET period_start=x'ff00ff'",
}


def _jan_feb(tmp_path: Path) -> tuple[Path, Path]:
    def month(first: str, last: str) -> fx.CASData:
        scheme = fx.scheme(fx.HDFC_MF, "Test Fund", "5", "5", [])
        return fx.rta(first, last, [fx.folio("1/1", "Test AMC", [scheme])])

    jan = fx.write(month("01-Jan-2026", "31-Jan-2026"), tmp_path / "jan.json")
    feb = fx.write(month("01-Feb-2026", "28-Feb-2026"), tmp_path / "feb.json")
    return jan, feb


@pytest.mark.parametrize("damage", sorted(ROW_DAMAGE))
def test_a_damaged_statement_row_is_said_and_imports_still_report(
    tmp_path: Path, damage: str
) -> None:
    import sqlite3

    client = _client(tmp_path)
    jan, feb = _jan_feb(tmp_path)
    _upload(client, jan)
    conn = sqlite3.connect(tmp_path / "data" / "ledger.db")
    conn.execute(ROW_DAMAGE[damage])
    conn.commit()
    conn.close()
    r = client.get("/import")
    assert r.status_code == 200 and "could not be listed" in r.text and "Traceback" not in r.text
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with bad.open("rb") as fh:
        r = client.post("/import", files={"statement": ("bad.json", fh, "application/json")})
    assert r.status_code == 200 and "Not imported" in r.text
    with feb.open("rb") as fh:
        r = client.post("/import", files={"statement": ("feb.json", fh, "application/json")})
    assert r.status_code == 200 and "new facts" in r.text and "could not be listed" in r.text


def test_a_ledger_that_is_not_a_database_still_says_why_a_file_was_refused(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "ledger.db").write_bytes(b"not a database, just text, long enough" * 4)
    r = client.get("/import")
    assert r.status_code == 200 and "could not be listed" in r.text
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with bad.open("rb") as fh:
        r = client.post("/import", files={"statement": ("bad.json", fh, "application/json")})
    assert r.status_code == 200 and "Not imported" in r.text


def test_an_outdated_ledger_says_how_to_rebuild_on_the_import_page(tmp_path: Path) -> None:
    import sqlite3

    from stockin.sources import cas
    from stockin.store import LedgerWriter

    db = tmp_path / "data" / "ledger.db"
    rows = [fx.txn("2026-01-25", "DIVIDEND_REINVEST", "1", "80")]
    data = fx.rta(
        "01-Jan-2026",
        "31-Jan-2026",
        [fx.folio("1/1", "AMC", [fx.scheme(fx.HDFC_MF, "F", "0", "1", rows)])],
    )
    with LedgerWriter.open(db) as w:
        w.ingest(cas.from_casparser(data, "old"))
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    client = _client(tmp_path)
    r = client.get("/import")
    assert r.status_code == 200 and "import your statements again" in r.text
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with bad.open("rb") as fh:
        r = client.post("/import", files={"statement": ("bad.json", fh, "application/json")})
    assert r.status_code == 200 and "Not imported" in r.text  # the file's own reason


# ------------------------------------------------------ more of the picture


def test_every_gap_gets_its_sentence_and_the_grid_keeps_the_latest_months() -> None:
    ps = [
        Period(source="cams_kfin_cas", start=date(2024, 1, 1), end=date(2024, 1, 31)),
        Period(source="cams_kfin_cas", start=date(2026, 11, 1), end=date(2026, 11, 30)),
        Period(source="nsdl_cas", start=date(2026, 6, 1), end=date(2026, 6, 30)),
        Period(source="nsdl_cas", start=date(2026, 9, 1), end=date(2026, 9, 30)),
    ]
    cover = coverage(tuple(ps))
    assert cover is not None
    assert len(cover.months) == MAX_MONTHS and cover.months[-1] == "Nov 2026"
    assert cover.months[0] == "Dec 2024"  # the latest two years, not the first
    assert len(cover.gap_lines) == 2
    assert cover.gap_lines[0] == "No Demat (NSDL) statement for Jul 2026, Aug 2026."
    assert cover.gap_lines[1].startswith(
        "No Mutual funds (CAMS / KFintech) statement for Dec 2024,"
    )


def test_the_page_says_every_gap(tmp_path: Path) -> None:
    client = _client(tmp_path)

    def stmt(first: str, last: str) -> fx.CASData:
        scheme = fx.scheme(fx.HDFC_MF, "Test Fund", "5", "5", [])
        return fx.rta(first, last, [fx.folio("1/1", "Test AMC", [scheme])])

    for n, (a, b) in enumerate(
        [
            ("01-Jan-2026", "31-Jan-2026"),
            ("01-Mar-2026", "31-Mar-2026"),
            ("01-May-2026", "31-May-2026"),
        ]
    ):
        _upload(client, fx.write(stmt(a, b), tmp_path / f"s{n}.json"))
    page = client.get("/import").text
    said = re.findall(r'<p class="small">.*?</p>', page, re.S)
    line = "No Mutual funds (CAMS / KFintech) statement for Feb 2026, Apr 2026."
    assert sum(line in p for p in said) == 1


def test_all_checks_opens_only_when_something_disagrees(tmp_path: Path) -> None:
    client = _client(tmp_path)
    jan, feb = _jan_feb(tmp_path)
    _upload(client, jan)
    _upload(client, feb)
    page = client.get("/reconcile").text
    assert '<details class="panel all-checks">' in page  # all agree: closed
    other = _client(tmp_path / "b")
    jan10 = fx.rta(
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
    feb7 = fx.rta(
        "01-Feb-2026",
        "28-Feb-2026",
        [fx.folio("1/1", "Test AMC", [fx.scheme(fx.HDFC_MF, "Test Fund", "7", "7", [])])],
    )
    (tmp_path / "b").mkdir(exist_ok=True)
    _upload(other, fx.write(jan10, tmp_path / "b" / "jan.json"))
    _upload(other, fx.write(feb7, tmp_path / "b" / "feb.json"))
    page = other.get("/reconcile").text
    assert '<details class="panel all-checks" open>' in page


def test_random_byte_damage_to_the_ledger_never_breaks_the_import_page(tmp_path: Path) -> None:
    client = _client(tmp_path)
    jan, feb = _jan_feb(tmp_path)
    _upload(client, jan)
    _upload(client, feb)
    db = tmp_path / "data" / "ledger.db"
    good = db.read_bytes()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    rnd = random.Random(36)
    for _ in range(60):
        data = bytearray(good)
        for _ in range(rnd.randint(1, 4)):
            data[rnd.randrange(len(data))] = rnd.randrange(256)
        db.write_bytes(bytes(data))
        r = client.get("/import")
        assert r.status_code in (200, 409) and "Traceback" not in r.text
        with bad.open("rb") as fh:
            r = client.post("/import", files={"statement": ("bad.json", fh, "application/json")})
        assert r.status_code == 200 and "Not imported" in r.text
