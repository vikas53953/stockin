import io
import json
import sys
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stockin.cli import app
from stockin.facts import parse_isin
from stockin.money import Money, Price, Qty
from stockin.prices import PriceBook, PriceFile, PriceStore, Quote, india_today
from stockin.sources import eod

FIX = Path(__file__).parent / "fixtures"
EOD = FIX / "eod"
runner = CliRunner()
TODAY = date(2026, 11, 18)  # the fixtures are dated 14 Nov 2026
NAVALL = (EOD / "NAVAll-sample.txt").read_text()
BHAV = (EOD / "bhavcopy-sample.csv").read_bytes()


def test_amfi_reads_nav_and_date_and_both_isin_columns() -> None:
    quotes = {q.isin: q for q in eod.parse_amfi(NAVALL, TODAY).quotes}
    flexi = quotes[parse_isin("INF179K01UT0")]
    assert flexi.price == Price.rupees("2100.1234")
    assert flexi.on == date(2026, 11, 14)
    assert flexi.source == "amfi"
    # payout and reinvestment ISINs share the NAV
    assert quotes[parse_isin("INF179K01VZ5")].price == Price.rupees("43.5")
    assert quotes[parse_isin("INF179K01VY8")].price == Price.rupees("43.5")
    # "N.A." is skipped, "-" is not an ISIN
    assert parse_isin("INF179K01BE2") not in quotes
    assert len(quotes) == 5


def test_amfi_rejects_a_file_that_is_not_navall() -> None:
    with pytest.raises(eod.EodRejected):
        eod.parse_amfi("hello;world\n", TODAY)


@pytest.mark.parametrize("name", ["bhavcopy-sample.csv", "bhavcopy-sample.csv.zip"])
def test_bhavcopy_reads_close_and_prefers_eq_series(name: str) -> None:
    file = eod.parse_bhavcopy((EOD / name).read_bytes(), TODAY)
    assert (file.source, file.day) == ("bhavcopy", date(2026, 11, 14))
    quotes = {q.isin: q for q in file.quotes}
    infy = quotes[parse_isin("INE009A01021")]
    assert infy.price == Price.rupees("1500.55")  # EQ, not the higher BL row
    assert infy.on == date(2026, 11, 14)
    assert infy.source == "bhavcopy"
    assert len(quotes) == 4


def test_bhavcopy_rejects_another_csv() -> None:
    with pytest.raises(eod.EodRejected):
        eod.parse_bhavcopy(b"a,b,c\n1,2,3\n", TODAY)


def _q(isin: str, on: date, price: str, source: str) -> Quote:
    return Quote(isin=parse_isin(isin), on=on, price=Price.rupees(price), source=source)  # type: ignore[arg-type]


def test_best_takes_the_latest_day_then_the_exchange_close() -> None:
    etf = "INF204KB14I2"
    older = _q(etf, date(2026, 11, 13), "280", "bhavcopy")
    nav = _q(etf, date(2026, 11, 14), "288.2", "amfi")
    close = _q(etf, date(2026, 11, 14), "287.61", "bhavcopy")
    book = PriceBook.best([older, nav, close])
    assert book.get(parse_isin(etf)) == close
    assert book.value(parse_isin(etf), Qty.parse("300")) == Money.rupees("86283.00")
    assert book.value(parse_isin("INE002A01018"), Qty.parse("1")) is None


def _file(*quotes: Quote, day: date | None = None) -> PriceFile:
    return PriceFile(source=quotes[0].source, day=day or quotes[0].on, quotes=quotes)


def test_store_is_idempotent_and_reads_on_or_before(tmp_path: Path) -> None:
    db = tmp_path / "prices.db"
    a = _q("INE009A01021", date(2026, 11, 13), "1490", "bhavcopy")
    b = _q("INE009A01021", date(2026, 11, 14), "1500.55", "bhavcopy")
    with PriceStore.open(db, write=True) as store:
        assert store.replace(_file(a)) == 1
        assert store.replace(_file(b)) == 1
        first = store.digest()
        assert store.replace(_file(b)) == 0
        assert store.replace(_file(a)) == 0  # an older day again does not move the book
        assert store.digest() == first
        assert store.book([a.isin]).get(a.isin) == b
        assert store.book([a.isin], on_or_before=date(2026, 11, 13)).get(a.isin) == a
        assert store.book([a.isin], on_or_before=date(2026, 11, 12)).get(a.isin) is None


def _ledger_with_mf_and_demat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    for name in ("mf-2026-01.json", "demat-2026-02.json"):
        result = runner.invoke(app, ["import", "cas", str(FIX / "cas" / name)])
        assert result.exit_code == 0, result.output


def test_prices_import_keeps_only_held_isins_and_values_holdings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    before = runner.invoke(app, ["holdings"])
    assert "no prices yet" in before.output

    result = runner.invoke(
        app, ["prices", "import", str(EOD / "bhavcopy-sample.csv.zip"), "--json"]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["read"] == 4
    assert report["kept"] == 1  # INFY only: TCS is sold out by Feb, the rest are not held
    again = json.loads(
        runner.invoke(app, ["prices", "import", str(EOD / "bhavcopy-sample.csv"), "--json"]).output
    )
    assert again["changed"] == 0  # the same prices again change nothing

    view = json.loads(runner.invoke(app, ["holdings", "--json"]).output)
    valued = {v["holding"]["isin"]: v for v in view["valued"]}
    infy = valued["INE009A01021"]
    qty = Qty.model_validate(infy["holding"]["qty"])
    assert Money.model_validate(infy["value"]) == Money.of(qty, Price.rupees("1500.55"))
    assert "INF179K01BE2" in report["unpriced"]  # the fund has no NAV yet

    nav = runner.invoke(app, ["prices", "import", str(EOD / "NAVAll-sample.txt"), "--json"])
    assert nav.exit_code == 0, nav.output
    assert "INF179K01BE2" in json.loads(nav.output)["unpriced"]  # N.A. in the file


def test_prices_import_rejects_another_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    junk = tmp_path / "junk.csv"
    junk.write_text("a,b\n1,2\n")
    result = runner.invoke(app, ["prices", "import", str(junk)])
    assert result.exit_code == 2
    assert "nothing was imported" in result.output
    assert not (tmp_path / "prices.db").exists()


def test_doctor_reports_prices_once_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    runner.invoke(app, ["prices", "import", str(EOD / "bhavcopy-sample.csv")])
    checks = {
        c["name"]: c for c in json.loads(runner.invoke(app, ["doctor", "--json"]).output)["checks"]
    }
    assert checks["quotes"]["status"] == "ok"
    assert "2026-11-14" in checks["quotes"]["detail"]


def test_gain_is_value_minus_cost_and_unknown_when_either_is() -> None:
    from stockin.book import Holding
    from stockin.cli import _valued  # pyright: ignore[reportPrivateUsage]
    from stockin.facts import AccountId

    isin = parse_isin("INE009A01021")
    book = PriceBook.best([_q("INE009A01021", date(2026, 11, 14), "150", "bhavcopy")])
    held = Holding(
        account=AccountId("a"), isin=isin, qty=Qty.parse("10"), cost=Money.rupees("1000")
    )
    v = _valued(held, book)
    assert v.value == Money.rupees("1500")
    assert v.gain == Money.rupees("500")
    assert _valued(held.model_copy(update={"cost": None}), book).gain is None
    assert _valued(held, PriceBook(quotes={})).gain is None


def test_a_dropped_connection_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Resp:
        def __enter__(self) -> "Resp":
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

    def flaky(req: object, timeout: float) -> Resp:
        calls.append("x")
        if len(calls) < 3:
            raise ConnectionResetError(104, "Connection reset by peer")
        return Resp()

    monkeypatch.setattr(eod.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(eod, "_sleep", lambda s: None)
    assert eod._get("https://example.invalid/x") == b"ok"  # pyright: ignore[reportPrivateUsage]
    assert len(calls) == 3


def test_fetch_keeps_what_downloaded_when_one_source_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    bhav = eod.parse_bhavcopy(BHAV, TODAY)

    def down(today: date) -> PriceFile:
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(eod, "fetch_amfi", down)
    monkeypatch.setattr(eod, "fetch_bhavcopy", lambda today: bhav)
    result = runner.invoke(app, ["prices", "fetch", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["kept"] == 1
    assert report["failed"] and report["failed"][0].startswith("AMFI")

    monkeypatch.setattr(eod, "fetch_bhavcopy", down)
    both = runner.invoke(app, ["prices", "fetch"])
    assert both.exit_code == 2
    assert "could not download prices" in both.output


def test_prices_print_exactly_with_two_decimals_at_least() -> None:
    from stockin.cli import _price_text  # pyright: ignore[reportPrivateUsage]

    def q(p: str) -> Quote:
        return _q("INE009A01021", date(2026, 11, 14), p, "bhavcopy")

    assert _price_text(q("1367")) == "1,367.00"
    assert _price_text(q("2378.851")) == "2,378.851"
    assert _price_text(q("94.4817")) == "94.4817"
    assert _price_text(q("0.5")) == "0.50"


class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_a_download_cut_off_midway_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    calls: list[str] = []

    class Cut(_Resp):
        def read(self) -> bytes:
            raise http.client.IncompleteRead(b"PK\x03", 9000)

    def flaky(req: object, timeout: float) -> _Resp:
        calls.append("x")
        return Cut(b"") if len(calls) < 2 else _Resp(b"ok")

    monkeypatch.setattr(eod.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(eod, "_sleep", lambda s: None)
    assert eod._get("https://example.invalid/x") == b"ok"  # pyright: ignore[reportPrivateUsage]
    assert len(calls) == 2


def _damaged_zip() -> bytes:
    whole = (EOD / "bhavcopy-sample.csv.zip").read_bytes()
    return whole[: len(whole) // 2]


@pytest.mark.parametrize(
    "data",
    [_damaged_zip(), b"PK\x03\x04" + b"\x00" * 40, b"TradDt,ISIN\n\xff\xfe,x\n"],
    ids=["truncated-zip", "junk-zip", "not-utf8"],
)
def test_a_damaged_bhavcopy_is_rejected_not_a_traceback(data: bytes) -> None:
    with pytest.raises(eod.EodRejected):
        eod.parse_bhavcopy(data, TODAY)


def test_import_of_a_truncated_zip_exits_2_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    bad = tmp_path / "cut.csv.zip"
    bad.write_bytes(_damaged_zip())
    result = runner.invoke(app, ["prices", "import", str(bad)])
    assert result.exit_code == 2, result.output
    assert "nothing was imported" in result.output
    assert not (tmp_path / "prices.db").exists()


def test_fetch_keeps_amfi_when_the_nse_download_is_damaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    amfi = eod.parse_amfi(NAVALL, TODAY)
    monkeypatch.setattr(eod, "fetch_amfi", lambda today: amfi)
    monkeypatch.setattr(eod, "_get", lambda url: _damaged_zip())
    result = runner.invoke(app, ["prices", "fetch", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["read"] == len(amfi.quotes) > 0  # the AMFI prices were not thrown away
    assert len(report["failed"]) == 1 and report["failed"][0].startswith("NSE")


def test_an_empty_price_file_is_rejected() -> None:
    with pytest.raises(eod.EodRejected):
        eod.parse_amfi(NAVALL.splitlines()[0] + "\n", TODAY)
    with pytest.raises(eod.EodRejected):
        eod.parse_bhavcopy(b"TradDt,ISIN,SctySrs,ClsPric\n", TODAY)


def test_value_rounds_half_even_to_the_paisa() -> None:
    isin = "INF179K01BB8"
    book = PriceBook.best([_q(isin, date(2026, 11, 14), "2378.851", "amfi")])
    # 1000.648 x 2378.851 = 2380392.495448: rounds up to .50, truncation gives .49
    assert book.value(parse_isin(isin), Qty.parse("1000.648")) == Money.rupees("2380392.50")
    half = PriceBook.best([_q(isin, date(2026, 11, 14), "5", "amfi")])
    # 0.003 x 5 = 0.015 (half): to even .02; 0.001 x 5 = 0.005: to even .00
    assert half.value(parse_isin(isin), Qty.parse("0.003")) == Money.rupees("0.02")
    assert half.value(parse_isin(isin), Qty.parse("0.001")) == Money.rupees("0")


def test_doctor_shows_the_latest_price_day_not_the_earliest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    with PriceStore.open(tmp_path / "prices.db", write=True) as store:
        store.replace(_file(_q("INE009A01021", date(2026, 11, 3), "1400", "bhavcopy")))
        store.replace(_file(_q("INE009A01021", date(2026, 11, 14), "1500", "bhavcopy")))
    checks = {
        c["name"]: c for c in json.loads(runner.invoke(app, ["doctor", "--json"]).output)["checks"]
    }
    assert "latest 2026-11-14" in checks["quotes"]["detail"]
    assert "2 end-of-day prices" in checks["quotes"]["detail"]


def test_a_holiday_404_looks_back_and_weekends_are_never_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    def get(url: str) -> bytes | None:
        ymd = url.split("_0_0_0_")[1][:8]
        asked.append(ymd)
        return _bhav_for("2026-11-12") if ymd == "20261112" else None  # Fri a holiday

    monkeypatch.setattr(eod, "_get", get)
    # Monday 16 Nov: Mon 404, Sun and Sat skipped, Fri 404, Thu found.
    assert eod.fetch_bhavcopy(date(2026, 11, 16)).day == date(2026, 11, 12)
    assert asked == ["20261116", "20261113", "20261112"]


def _bhav_for(day: str) -> bytes:
    """The fixture bhavcopy as NSE would publish it for another day."""
    assert b"2026-11-14" in BHAV
    return BHAV.replace(b"2026-11-14", day.encode())


def test_a_bhavcopy_for_another_day_than_the_one_asked_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(eod, "_get", lambda url: _bhav_for("2026-11-12"))
    with pytest.raises(eod.EodRejected, match="for 2026-11-16 is dated 2026-11-12"):
        eod.fetch_bhavcopy(date(2026, 11, 16))


def test_a_week_with_no_nse_file_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eod, "_get", lambda url: None)
    with pytest.raises(eod.EodRejected, match="no NSE bhavcopy"):
        eod.fetch_bhavcopy(date(2026, 11, 16))


def test_an_empty_prices_db_means_no_prices_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    (tmp_path / "prices.db").write_bytes(b"")
    doctor = runner.invoke(app, ["doctor", "--json"])
    checks = {c["name"]: c for c in json.loads(doctor.output)["checks"]}
    assert checks["quotes"]["detail"] == "no prices yet"
    holdings = runner.invoke(app, ["holdings"])
    assert holdings.exit_code == 0, holdings.output
    assert "no prices yet" in holdings.output


def test_prices_db_is_private(tmp_path: Path) -> None:
    import os
    import stat

    path = tmp_path / "prices.db"
    with PriceStore.open(path, write=True):
        pass
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_fetch_keeps_amfi_when_nse_keeps_cutting_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import http.client

    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    amfi = eod.parse_amfi(NAVALL, TODAY)

    def cut(req: object, timeout: float) -> _Resp:
        raise http.client.IncompleteRead(b"PK", 9000)

    monkeypatch.setattr(eod, "fetch_amfi", lambda today: amfi)
    monkeypatch.setattr(eod.urllib.request, "urlopen", cut)
    monkeypatch.setattr(eod, "_sleep", lambda s: None)
    result = runner.invoke(app, ["prices", "fetch", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["read"] == len(amfi.quotes)
    assert len(report["failed"]) == 1 and report["failed"][0].startswith("NSE")
    assert "unexpected" not in report["failed"][0]  # a known failure, said plainly


def _asked_day(url: str) -> str:
    ymd = url.split("_0_0_0_")[1][:8]
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"


def _foreign_sqlite(path: Path) -> None:
    import sqlite3

    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE quotes(a, b)")


@pytest.mark.parametrize("kind", ["text", "other-sqlite"])
def test_a_prices_db_we_did_not_make_is_reported_and_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    db = tmp_path / "prices.db"
    if kind == "text":
        db.write_bytes(b"not a database at all, just text, long enough to have a header")
    else:
        _foreign_sqlite(db)
    before = db.read_bytes()
    checks = {
        c["name"]: c for c in json.loads(runner.invoke(app, ["doctor", "--json"]).output)["checks"]
    }
    assert checks["quotes"]["status"] == "fail"
    assert "not a Stockin price file" in checks["quotes"]["detail"]
    for args in (
        ["holdings"],
        ["prices", "import", str(EOD / "bhavcopy-sample.csv")],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 2, (args, result.output)
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "move prices.db aside" in result.output
    assert db.read_bytes() == before


def test_a_404_from_the_server_means_no_file_that_day(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    calls: list[str] = []

    def answer(code: int):  # type: ignore[no-untyped-def]
        def urlopen(req: object, timeout: float) -> _Resp:
            calls.append("x")
            raise urllib.error.HTTPError("https://x", code, "no", {}, None)  # type: ignore[arg-type]

        return urlopen

    monkeypatch.setattr(eod, "_sleep", lambda s: None)
    monkeypatch.setattr(eod.urllib.request, "urlopen", answer(404))
    assert eod._get("https://example.invalid/x") is None  # pyright: ignore[reportPrivateUsage]
    assert len(calls) == 1  # a 404 is an answer, not retried
    calls.clear()
    monkeypatch.setattr(eod.urllib.request, "urlopen", answer(403))
    with pytest.raises(urllib.error.HTTPError):
        eod._get("https://example.invalid/x")  # pyright: ignore[reportPrivateUsage]
    assert len(calls) == 1  # a 4xx other than 404 is not retried either


def test_retries_wait_2_then_4_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    def down(req: object, timeout: float) -> _Resp:
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(eod.urllib.request, "urlopen", down)
    monkeypatch.setattr(eod, "_sleep", waits.append)
    with pytest.raises(ConnectionResetError):
        eod._get("https://example.invalid/x")  # pyright: ignore[reportPrivateUsage]
    assert waits == [2, 4]


def test_the_lookback_reaches_a_full_week(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url: str) -> bytes | None:
        # only the Monday a week ago
        return _bhav_for("2026-11-09") if "_20261109_" in url else None

    monkeypatch.setattr(eod, "_get", get)
    assert eod.fetch_bhavcopy(date(2026, 11, 16)).quotes


def _zip_edits() -> dict[str, bytes]:
    whole = bytearray((EOD / "bhavcopy-sample.csv.zip").read_bytes())
    out: dict[str, bytes] = {}
    enc = bytearray(whole)
    enc[6] |= 0x01  # local header general-purpose flag: "encrypted"
    cd = whole.rfind(b"PK\x01\x02")
    enc[cd + 8] |= 0x01  # and the same flag in the central directory
    out["encrypted-flag"] = bytes(enc)
    bad_off = bytearray(whole)
    eocd = whole.rfind(b"PK\x05\x06")
    bad_off[eocd + 16 : eocd + 20] = (0xFFFFFFF0).to_bytes(4, "little")
    out["central-directory-offset"] = bytes(bad_off)
    flipped = bytearray(whole)
    name_len = int.from_bytes(whole[26:28], "little")
    extra_len = int.from_bytes(whole[28:30], "little")
    start = 30 + name_len + extra_len
    flipped[start + 3] ^= 0x01  # the fixture is stored: a changed byte fails the CRC
    out["stored-byte-crc"] = bytes(flipped)
    # NSE deflates its zips: a reserved block type is a broken stream (zlib.error)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("BhavCopy.csv", (EOD / "bhavcopy-sample.csv").read_bytes())
    deflated = bytearray(buf.getvalue())
    name_len = int.from_bytes(deflated[26:28], "little")
    extra_len = int.from_bytes(deflated[28:30], "little")
    deflated[30 + name_len + extra_len] = 0xFF  # BFINAL=1, BTYPE=11 (reserved)
    out["deflate-stream"] = bytes(deflated)
    return out


@pytest.mark.parametrize("name", sorted(_zip_edits()))
def test_damaged_zip_headers_and_streams_are_rejected(name: str) -> None:
    with pytest.raises(eod.EodRejected):
        eod.parse_bhavcopy(_zip_edits()[name], TODAY)


def test_every_single_byte_flip_of_a_bhavcopy_zip_parses_or_is_rejected() -> None:
    whole = (EOD / "bhavcopy-sample.csv.zip").read_bytes()
    good = eod.parse_bhavcopy(whole, TODAY)
    for i in range(len(whole)):
        for bit in (0x01, 0x80):
            data = bytearray(whole)
            data[i] ^= bit
            try:
                eod.parse_bhavcopy(bytes(data), TODAY)
            except eod.EodRejected:
                continue
            # accepted: only bytes the zip format ignores (times, comments) may change
            assert eod.parse_bhavcopy(bytes(data), TODAY) == good, (i, bit)


def test_a_csv_cut_off_mid_row_is_rejected() -> None:
    text = (EOD / "bhavcopy-sample.csv").read_bytes()
    with pytest.raises(eod.EodRejected, match="mid-row"):
        eod.parse_bhavcopy(text[:-7], TODAY)


def test_a_navall_saved_with_a_byte_order_mark_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    nav = tmp_path / "NAVAll-bom.txt"
    nav.write_bytes(b"\xef\xbb\xbf" + (EOD / "NAVAll-sample.txt").read_bytes())
    result = runner.invoke(app, ["prices", "import", str(nav), "--json"])
    assert result.exit_code == 0, result.output
    assert "(amfi)" in json.loads(result.output)["source"]


def test_an_unexpected_error_in_one_source_keeps_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    amfi = eod.parse_amfi(NAVALL, TODAY)

    def boom(today: date) -> PriceFile:
        raise KeyError("something nobody planned for")

    monkeypatch.setattr(eod, "fetch_amfi", lambda today: amfi)
    monkeypatch.setattr(eod, "fetch_bhavcopy", boom)
    result = runner.invoke(app, ["prices", "fetch", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["read"] == len(amfi.quotes)
    assert report["failed"] == ["NSE: unexpected KeyError: 'something nobody planned for'"]


def _one_byte_damage(whole: bytes) -> Iterator[tuple[int, bytes]]:
    """(byte, damaged file) for every byte and four kinds of change: CR,
    quote, NUL and one flipped bit."""
    for i in range(len(whole)):
        for new in (0x0D, 0x22, 0x00, whole[i] ^ 0x01):
            data = bytearray(whole)
            data[i] = new
            yield i, bytes(data)


def _parse(name: str, data: bytes) -> PriceFile:
    """A fixture file read the way `prices import` and `prices fetch` read it."""
    if name.startswith("NAVAll"):
        return eod.parse_amfi(data.decode("utf-8-sig", errors="replace"), TODAY)
    return eod.parse_bhavcopy(data, TODAY)


def _a_day_earlier(file: PriceFile) -> PriceFile:
    back = timedelta(days=1)
    quotes = tuple(q.model_copy(update={"on": q.on - back}) for q in file.quotes)
    return PriceFile(source=file.source, day=file.day - back, quotes=quotes)


@pytest.mark.parametrize("name", ["bhavcopy-sample.csv", "NAVAll-sample.txt"])
def test_every_single_byte_change_is_rejected_or_undone_by_the_right_file(
    tmp_path: Path, name: str
) -> None:
    """Verifier 5: neither plain file has a checksum, so a damaged copy can be
    a valid file with a wrong price or date (also for an ISIN on another
    line), and prices.db kept it for good. The bar: every damaged copy is
    rejected, or importing the right file afterwards leaves prices.db exactly
    as if the damaged one had never been imported."""
    whole = (EOD / name).read_bytes()
    clean = _parse(name, whole)
    stored_wrong = 0
    with PriceStore.open(tmp_path / "prices.db", write=True) as store:
        store.take(_a_day_earlier(clean))  # yesterday's prices: the jump check has a base
        assert store.take(clean) == (len(clean.quotes), ())
        want = store.digest()
        for i, data in _one_byte_damage(whole):
            try:
                damaged = _parse(name, data)
            except eod.EodRejected:
                continue
            store.take(damaged)
            stored_wrong += store.digest() != want
            assert store.take(clean)[1] == (), i
            assert store.digest() == want, (i, data[max(0, i - 40) : i + 40])
    assert stored_wrong > 50  # the test is about damage that does get in


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"OpnPric,HghPric", b"OpnPric-HghPric"),  # verifier 4: a header comma lost
        (b"HghPric,LwPric", b"HghPric,,LwPric"),  # one added
        (b"INFY LTD,1,1,1,1500.55", b"INFY LTD,1,1,1500.55"),  # one lost in a row
    ],
    ids=["header-comma-lost", "header-comma-added", "row-comma-lost"],
)
def test_a_shifted_column_is_rejected_not_stored_as_the_close(old: bytes, new: bytes) -> None:
    whole = (EOD / "bhavcopy-sample.csv").read_bytes()
    assert old in whole
    with pytest.raises(eod.EodRejected, match="fields but the header has"):
        eod.parse_bhavcopy(whole.replace(old, new, 1), TODAY)


def test_a_stray_quote_cannot_swallow_the_rows_after_it() -> None:
    whole = (EOD / "bhavcopy-sample.csv").read_bytes()
    first_row_end = whole.index(b"\n", whole.index(b"\n") + 1)
    damaged = whole[:first_row_end] + b'"' + whole[first_row_end + 1 :]
    with pytest.raises(eod.EodRejected):
        eod.parse_bhavcopy(damaged, TODAY)


def test_a_quoted_field_spanning_rows_is_rejected() -> None:
    """Two stray quotes can wrap a whole row into one field and keep the
    field count right; no NSE field spans lines."""
    whole = (EOD / "bhavcopy-sample.csv").read_bytes()
    lines = whole.split(b"\n")
    lines[1] = lines[1][:-1] + b'"'  # the last (empty) field opens a quote...
    lines[2] = lines[2] + b'"'  # ...that closes at the end of the next row
    with pytest.raises(eod.EodRejected, match="runs over several lines"):
        eod.parse_bhavcopy(b"\n".join(lines), TODAY)


def test_text_after_a_closing_quote_is_rejected_not_glued_on() -> None:
    """The lenient CSV reader would read "1500.55"5 as 1500.555."""
    whole = (EOD / "bhavcopy-sample.csv").read_bytes()
    damaged = whole.replace(b",1500.55,1500.55,", b',"1500.55"5,1500.55,', 1)
    assert damaged != whole
    with pytest.raises(eod.EodRejected, match="damaged"):
        eod.parse_bhavcopy(damaged, TODAY)


def test_two_navall_rows_run_together_are_rejected() -> None:
    text = (EOD / "NAVAll-sample.txt").read_text()
    joined = text.replace("14-Nov-2026\n118969", "14-Nov-2026\x00118969", 1)
    assert joined != text
    with pytest.raises(eod.EodRejected, match="fields but the header has"):
        eod.parse_amfi(joined, TODAY)


def test_columns_are_found_by_name_in_any_order() -> None:
    """Verifier 4's crafted file: TradDt last, then a short row. Columns in
    any order parse; the short row is rejected, not an AttributeError."""
    good = b"ISIN,ClsPric,SctySrs,TradDt\nINE009A01021,1500.55,EQ,2026-11-14\n"
    (q,) = eod.parse_bhavcopy(good, TODAY).quotes
    assert (q.isin, q.price, q.on) == ("INE009A01021", Price.rupees("1500.55"), date(2026, 11, 14))
    with pytest.raises(eod.EodRejected, match="row 3 has 2 fields"):
        eod.parse_bhavcopy(good + b"INE009A01021,1500.55\n", TODAY)
    with pytest.raises(eod.EodRejected, match="names a column twice"):
        eod.parse_bhavcopy(b"ISIN,ClsPric,SctySrs,TradDt,ISIN\nA,1,EQ,2026-11-14,B\n", TODAY)


@pytest.mark.parametrize("source", ["nse", "amfi"])
def test_a_price_beyond_any_real_price_rejects_the_file(source: str) -> None:
    """Verifier 4: a 14-digit close crashed SQLite with OverflowError. ₹1
    crore a unit is the ceiling; one cell above it rejects the file."""
    huge = "12345678901234.5"
    if source == "nse":
        data = b"TradDt,ISIN,SctySrs,ClsPric\n2026-11-14,INE009A01021,EQ," + huge.encode() + b"\n"
        with pytest.raises(eod.EodRejected, match="beyond any real price"):
            eod.parse_bhavcopy(data, TODAY)
    else:
        text = (EOD / "NAVAll-sample.txt").read_text().replace("2100.1234", huge)
        with pytest.raises(eod.EodRejected, match="beyond any real price"):
            eod.parse_amfi(text, TODAY)
    edge = f"{eod.MAX_PRICE_MICROS // 10**6}"
    row = b"TradDt,ISIN,SctySrs,ClsPric\n2026-11-14,INE009A01021,EQ," + edge.encode() + b"\n"
    assert eod.parse_bhavcopy(row, TODAY).quotes  # exactly the ceiling is still a price
    with pytest.raises(ValueError):
        Quote(
            isin=parse_isin("INE009A01021"),
            on=date(2026, 11, 14),
            price=Price(micro_rupees=eod.MAX_PRICE_MICROS + 1),
            source="bhavcopy",
        )


def test_fetch_keeps_amfi_when_the_nse_file_has_an_absurd_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    navall = (EOD / "NAVAll-sample.txt").read_bytes()
    bad = b"TradDt,ISIN,SctySrs,ClsPric\n%s,INE009A01021,EQ,99999999999999\n"
    monkeypatch.setattr(
        eod, "_get", lambda url: navall if "amfi" in url else bad % _asked_day(url).encode()
    )
    result = runner.invoke(app, ["prices", "fetch", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["read"] == len(eod.parse_amfi(NAVALL, TODAY).quotes)  # AMFI's prices survived
    assert len(report["failed"]) == 1 and "beyond any real price" in report["failed"][0]


@pytest.mark.parametrize(
    "data",
    [
        b"\r" + (EOD / "bhavcopy-sample.csv").read_bytes()[1:],
        b'TradDt,ISIN,SctySrs,ClsPric\n2026-11-14,INE009A01021,EQ,"' + b"9" * 200_000 + b"\n",
    ],
    ids=["bare-cr", "field-too-large"],
)
def test_a_csv_the_csv_module_cannot_read_is_rejected(data: bytes) -> None:
    with pytest.raises(eod.EodRejected):
        eod.parse_bhavcopy(data, TODAY)


def test_a_sqlite_prices_db_without_our_table_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    db = tmp_path / "prices.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE notes(x)")
    before = db.read_bytes()
    result = runner.invoke(app, ["prices", "import", str(EOD / "bhavcopy-sample.csv")])
    assert result.exit_code == 2, result.output
    assert db.read_bytes() == before


def test_fetch_amfi_reads_a_navall_with_a_byte_order_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"\xef\xbb\xbf" + (EOD / "NAVAll-sample.txt").read_bytes()
    monkeypatch.setattr(eod, "_get", lambda url: body)
    assert eod.fetch_amfi(TODAY) == eod.parse_amfi(NAVALL, TODAY)


def _store_with_rows(path: Path, n: int = 400) -> None:
    """A real prices.db, big enough to span several pages."""
    with PriceStore.open(path, write=True) as store:
        store.replace(
            _file(
                *(
                    _q(_with_digit(f"INE{i:08d}"), date(2026, 11, 14), "100", "bhavcopy")
                    for i in range(n)
                )
            )
        )


def _with_digit(body: str) -> str:
    for d in "0123456789":
        try:
            return parse_isin(body + d)
        except ValueError:
            continue
    raise AssertionError(body)


_TOMORROW = (india_today() + timedelta(days=1)).isoformat()
_ROWS = {
    "text-price": ("INE009A01021", "bhavcopy", "2026-11-14", "2026-11-14", "lots"),
    "bad-date": ("INE009A01021", "bhavcopy", "2026-11-14", "14/11/2026", 1),
    "unknown-source": ("INE009A01021", "broker", "2026-11-14", "2026-11-14", 1),
    "no-primary-key": ("INE009A01021", "bhavcopy", "2026-11-14", "2026-11-14", 1),
    # verifier 5: each of these passed when its own clause of the scan was removed
    "isin-not-text": (123456789012, "bhavcopy", "2026-11-14", "2026-11-14", 1),
    "isin-short": ("INE009A0102", "bhavcopy", "2026-11-14", "2026-11-14", 1),
    "price-not-integer": ("INE009A01021", "bhavcopy", "2026-11-14", "2026-11-14", 1.5),
    "price-zero": ("INE009A01021", "bhavcopy", "2026-11-14", "2026-11-14", 0),
    "price-above-ceiling": (
        "INE009A01021",
        "bhavcopy",
        "2026-11-14",
        "2026-11-14",
        eod.MAX_PRICE_MICROS + 1,
    ),
    "date-not-text": ("INE009A01021", "amfi", "2026-11-14", 20261114, 1),
    # verifier 5 (N2): month 13 passed a digits-only check, then crashed the read
    "month-13": ("INE009A01021", "amfi", "2026-11-14", "2026-13-39", 1),
    "30-february": ("INE009A01021", "amfi", "2026-11-14", "2026-02-30", 1),
    "file-day-month-13": ("INE009A01021", "amfi", "2026-13-39", "2026-11-14", 1),
    "bhavcopy-day-differs": ("INE009A01021", "bhavcopy", "2026-11-14", "2026-11-13", 1),
    # verifier 6 (F1): a row dated after today would be the newest file for ever,
    # and no later import could replace it
    "file-day-after-today": ("INE009A01021", "amfi", _TOMORROW, "2026-11-14", 1),
    "on-date-after-today": ("INE009A01021", "amfi", "2026-11-14", _TOMORROW, 1),
    "one-digit-to-2029": ("INE009A01021", "bhavcopy", "2029-11-17", "2029-11-17", 1),
}


def _damage(kind: str, db: Path) -> None:
    import os
    import sqlite3

    if kind == "second-page":
        _store_with_rows(db)
        data = bytearray(db.read_bytes())
        page = int.from_bytes(data[16:18], "big")
        data[page : page * 2] = b"\xab" * page  # page 2 overwritten
        db.write_bytes(bytes(data))
    elif kind == "directory":
        db.mkdir()
    elif kind == "fifo":
        os.mkfifo(db)
    elif kind == "older-stockin":  # the table this branch wrote before files replaced days
        with sqlite3.connect(db) as c:
            c.execute(
                "CREATE TABLE quotes(isin, on_date, source, micro_rupees,"
                " PRIMARY KEY(isin, on_date, source))"
            )
            c.execute("INSERT INTO quotes VALUES ('INE009A01021', '9026-11-17', 'bhavcopy', 1)")
    else:
        with sqlite3.connect(db) as c:
            pk = "" if kind == "no-primary-key" else ", PRIMARY KEY(isin, source, file_day)"
            c.execute(f"CREATE TABLE quotes(isin, source, file_day, on_date, micro_rupees{pk})")
            c.execute("INSERT INTO quotes VALUES (?,?,?,?,?)", _ROWS[kind])


def test_a_row_stockin_writes_passes_the_scan(tmp_path: Path) -> None:
    """The control for the cases below: the same hand-made table with a good
    row is read, so each case fails for its own row, not for the table."""
    import sqlite3

    db = tmp_path / "prices.db"
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE quotes(isin, source, file_day, on_date, micro_rupees,"
            " PRIMARY KEY(isin, source, file_day))"
        )
        c.execute(
            "INSERT INTO quotes VALUES (?,?,?,?,?)",
            ("INE009A01021", "amfi", "2026-11-14", "2026-11-13", eod.MAX_PRICE_MICROS),
        )
    with PriceStore.open(db) as store:
        quote = store.book([parse_isin("INE009A01021")]).get(parse_isin("INE009A01021"))
    assert quote == Quote(
        isin=parse_isin("INE009A01021"),
        on=date(2026, 11, 13),
        price=Price(micro_rupees=eod.MAX_PRICE_MICROS),
        source="amfi",
    )


@pytest.mark.parametrize(
    "kind",
    [
        "second-page",
        "directory",
        pytest.param("fifo", marks=pytest.mark.skipif(sys.platform == "win32", reason="no FIFO")),
        "older-stockin",
        *_ROWS,
    ],
)
def test_a_damaged_or_foreign_prices_db_is_reported_and_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Verifier 4: a damaged second page crashed doctor, holdings and import
    with DatabaseError (and import rewrote the file); a foreign file with our
    column names passed the shape check; a directory crashed import.
    Verifier 5: with a directory or FIFO, doctor said FAIL while holdings
    said "no prices yet" with exit 0. All three commands now agree."""
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    db = tmp_path / "prices.db"
    _damage(kind, db)
    before = db.read_bytes() if db.is_file() else None
    checks = json.loads(runner.invoke(app, ["doctor", "--json"]).output)["checks"]
    (quotes,) = [c for c in checks if c["name"] == "quotes"]
    assert quotes["status"] == "fail", quotes
    for args in (["prices", "import", str(EOD / "bhavcopy-sample.csv")], ["holdings"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 2, (args, result.output)
        assert result.exception is None or isinstance(result.exception, SystemExit), result.output
        assert "move prices.db aside" in result.output
    if before is not None:
        assert db.read_bytes() == before
    assert not list(tmp_path.glob("prices.db-*"))  # no WAL or journal left beside it


# ---------------------------------------------- a file replaces its day (verifier 5)


def test_the_last_import_of_a_day_wins_whatever_it_says(tmp_path: Path) -> None:
    """prices.db used to keep the larger of two prices for a day, so a
    too-high price from a damaged file could never be corrected."""
    infy, tcs = "INE009A01021", "INE467B01029"
    day = date(2026, 11, 14)
    high = _file(_q(infy, day, "9500.55", "bhavcopy"), _q(tcs, day, "3333.10", "bhavcopy"))
    right = _file(_q(infy, day, "1500.55", "bhavcopy"))
    nav = _file(_q(infy, day, "7", "amfi"))
    earlier = _file(_q(tcs, date(2026, 11, 13), "3300", "bhavcopy"))
    with PriceStore.open(tmp_path / "prices.db", write=True) as store:
        for other in (nav, earlier):
            store.replace(other)
        assert store.replace(high) == 2
        assert store.replace(right) == 2  # one price lowered, one row removed
        isins = [parse_isin(infy), parse_isin(tcs)]
        book = store.book(isins)
        assert book.get(isins[0]) == right.quotes[0]
        # TCS was only in the first file: its row for the day is gone, the earlier day stays
        assert book.get(isins[1]) == earlier.quotes[0]
        assert store.stored("bhavcopy", day) == {isins[0]: right.quotes[0]}
        assert store.stored("amfi", day) == {isins[0]: nav.quotes[0]}  # another source's day
        assert store.count() == 3


def test_the_book_reads_each_sources_newest_file(tmp_path: Path) -> None:
    """A NAVAll row can be days old. The newest file's row is the price, even
    when an older file (a damaged one) carried a later date for it."""
    fund = "INF179K01BE2"
    old_file = _file(_q(fund, date(2026, 11, 13), "50", "amfi"), day=date(2026, 11, 13))
    new_file = _file(_q(fund, date(2026, 11, 12), "40", "amfi"), day=date(2026, 11, 14))
    with PriceStore.open(tmp_path / "prices.db", write=True) as store:
        store.replace(new_file)
        store.replace(old_file)
        assert store.book([parse_isin(fund)]).get(parse_isin(fund)) == new_file.quotes[0]
        assert store.latest_day() == date(2026, 11, 14)


# ------------------------------------------------------------ dates (verifier 5)


def test_a_bhavcopy_dated_after_today_is_rejected() -> None:
    """Verifier 5: TradDt 9026-11-17 was stored, and no later file could ever
    replace it as the latest price."""
    assert eod.parse_bhavcopy(BHAV, date(2026, 11, 14)).day == date(2026, 11, 14)
    with pytest.raises(eod.EodRejected, match="dated 2026-11-14, after today"):
        eod.parse_bhavcopy(BHAV, date(2026, 11, 13))
    with pytest.raises(eod.EodRejected, match="after today"):
        eod.parse_bhavcopy(_bhav_for("9026-11-14"), TODAY)


def test_prices_import_rejects_a_file_dated_after_today_in_india(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifier 6 (N4): nothing checked that `prices import` passes the real
    day to the parser; passing date.max would have let a future file in."""
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    tomorrow = tmp_path / "tomorrow.csv"
    tomorrow.write_bytes(_bhav_for(_TOMORROW))
    result = runner.invoke(app, ["prices", "import", str(tomorrow)])
    assert result.exit_code == 2, result.output
    assert "after today" in result.output
    assert not (tmp_path / "prices.db").exists()
    tomorrow_nav = tmp_path / "NAVAll.txt"
    nav_day = date.fromisoformat(_TOMORROW).strftime("%d-%b-%Y")
    tomorrow_nav.write_text(_nav(f"1;INF179K01UT0;-;Fund;Direct Plan;Growth;10.5;{nav_day}"))
    result = runner.invoke(app, ["prices", "import", str(tomorrow_nav)])
    assert result.exit_code == 2, result.output
    assert "after today" in result.output
    assert not (tmp_path / "prices.db").exists()
    today = tmp_path / "today.csv"
    today.write_bytes(_bhav_for(india_today().isoformat()))
    assert runner.invoke(app, ["prices", "import", str(today)]).exit_code in (0, 1)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"2026-11-14,2026-11-14,CM,NSE,STK,1,INE467B01029", b"2028-11-14"),
        (b"2026-11-14,2026-11-14,CM,NSE,STK,1,INF204KB14I2", b"2026-11-13"),
        (b"2026-11-14,2026-11-14,CM,NSE,STK,1,INE009A01021,INFY,BL", b"2026-11-1x"),
    ],
    ids=["a-later-year", "a-day-earlier", "not-a-date"],
)
def test_a_bhavcopy_row_for_another_day_rejects_the_file(old: bytes, new: bytes) -> None:
    """A bhavcopy is one trading day: one row with another TradDt (even a
    row that is not the one used, like the BL row) is a damaged file."""
    assert BHAV.count(old) == 1
    with pytest.raises(eod.EodRejected, match="but the file is for 2026-11-14"):
        eod.parse_bhavcopy(BHAV.replace(old, new + old[10:]), TODAY)


def test_a_bhavcopy_whose_day_is_not_a_date_is_rejected() -> None:
    with pytest.raises(eod.EodRejected, match="TradDt '2026-13-39' is not a date"):
        eod.parse_bhavcopy(_bhav_for("2026-13-39"), TODAY)


def _nav(*rows: str) -> str:
    return "\n".join([NAVALL.splitlines()[0], "", "Some Mutual Fund", "", *rows, ""])


def test_a_nav_dated_after_today_is_rejected() -> None:
    row = "1;INF179K01UT0;-;Fund;Direct Plan;Growth;10.5;14-Nov-2026"
    assert eod.parse_amfi(_nav(row), date(2026, 11, 14)).day == date(2026, 11, 14)
    with pytest.raises(eod.EodRejected, match="line 5 is dated 2026-11-14, after today"):
        eod.parse_amfi(_nav(row), date(2026, 11, 13))


def test_a_nav_row_whose_date_is_not_a_date_is_rejected() -> None:
    """It used to be skipped: a held fund silently lost its price."""
    good = "1;INF179K01UT0;-;Fund;Direct Plan;Growth;10.5;14-Nov-2026"
    for bad in ("14-Nxv-2026", "31-Feb-2026", ""):
        row = f"2;INF179K01VY8;-;Fund;Direct Plan;Growth;43.5;{bad}"
        with pytest.raises(eod.EodRejected, match=r"line 6: .* is not a date"):
            eod.parse_amfi(_nav(good, row), TODAY)
    # a row with no NAV has no price to date: still skipped, as before
    no_nav = "2;INF179K01VY8;-;Fund;Direct Plan;Growth;N.A.;"
    assert len(eod.parse_amfi(_nav(good, no_nav), TODAY).quotes) == 1


def test_a_nav_absurdly_older_than_the_file_is_rejected() -> None:
    """NAVAll lists wound-up schemes (the real file reaches back 18 years),
    so old rows are normal; a year 30 below the file's is a damaged digit."""
    now = [
        f"{n};{isin};-;Fund;Direct Plan;Growth;10.5;14-Nov-2026"
        for n, isin in enumerate(("INF179K01UT0", "INF179K01VY8"))
    ]
    old = "9;INF879O01027;-;Fund;Direct Plan;Growth;94;14-Nov-{year}"
    file = eod.parse_amfi(_nav(*now, old.format(year=1996)), TODAY)
    assert min(q.on for q in file.quotes) == date(1996, 11, 14)  # 30 years: kept
    with pytest.raises(eod.EodRejected, match="dated 1995-11-14, more than 30 years"):
        eod.parse_amfi(_nav(*now, old.format(year=1995)), TODAY)
    with pytest.raises(eod.EodRejected, match="more than 30 years"):
        eod.parse_amfi(_nav(*now, old.format(year=1026)), TODAY)


def test_a_navalls_day_is_the_date_most_rows_carry() -> None:
    """One row with a later date (AMFI still updating, or a damaged digit)
    does not move the file's day, so the right file still replaces it."""

    def row(n: int, isin: str, on: str) -> str:
        return f"{n};{isin};-;Fund;Direct Plan;Growth;10.5;{on}"

    a, b, c = "INF179K01UT0", "INF179K01VY8", "INF879O01027"
    most = _nav(row(1, a, "13-Nov-2026"), row(2, b, "13-Nov-2026"), row(3, c, "17-Nov-2026"))
    assert eod.parse_amfi(most, TODAY).day == date(2026, 11, 13)
    tie = _nav(row(1, a, "13-Nov-2026"), row(2, b, "14-Nov-2026"))
    assert eod.parse_amfi(tie, TODAY).day == date(2026, 11, 14)  # a tie: the later
    assert eod.parse_amfi(NAVALL, TODAY).day == date(2026, 11, 14)


def test_an_isin_listed_twice_in_a_navall() -> None:
    """Verifier 5: one byte on another fund's line turned its ISIN into a
    held fund's, and the higher NAV was kept. AMFI's real file does list a few
    ISINs twice with different dates: the later one is the price. Two NAVs for
    one date cannot both be right, so neither is taken."""

    def row(isin: str, nav: str, on: str) -> str:
        return f"1;{isin};-;Fund;Direct Plan;Growth;{nav};{on}"

    held, other = parse_isin("INF179K01VY8"), "INF179K01UT0"
    stay = row(other, "10.5", "14-Nov-2026")
    later = _nav(stay, row(held, "43.488", "14-Nov-2026"), row(held, "47.541", "13-Nov-2026"))
    (got,) = [q for q in eod.parse_amfi(later, TODAY).quotes if q.isin == held]
    assert (got.price, got.on) == (Price.rupees("43.488"), date(2026, 11, 14))
    flipped = _nav(stay, row(held, "47.541", "13-Nov-2026"), row(held, "43.488", "14-Nov-2026"))
    assert eod.parse_amfi(flipped, TODAY) == eod.parse_amfi(later, TODAY)
    clash = _nav(stay, row(held, "43.488", "14-Nov-2026"), row(held, "47.541", "14-Nov-2026"))
    assert [q.isin for q in eod.parse_amfi(clash, TODAY).quotes] == [other]
    same = _nav(stay, row(held, "43.488", "14-Nov-2026"), row(held, "43.488", "14-Nov-2026"))
    assert len(eod.parse_amfi(same, TODAY).quotes) == 2  # said twice, said the same: fine
    # a later date settles a clash, and a clash after it is with that later date
    settled = _nav(
        stay,
        row(held, "1", "12-Nov-2026"),
        row(held, "2", "12-Nov-2026"),
        row(held, "43.488", "14-Nov-2026"),
    )
    assert eod.parse_amfi(settled, TODAY) == eod.parse_amfi(later, TODAY)


def _bhav_rows(*rows: str) -> bytes:
    return ("TradDt,ISIN,SctySrs,ClsPric\n" + "".join(f"2026-11-14,{r}\n" for r in rows)).encode()


def test_an_isin_with_two_closes_in_one_series_gets_no_price() -> None:
    """The bhavcopy twin of the NAVAll case: an ISIN damaged into another
    one's gives that one two EQ rows. Neither close is taken."""
    infy, tcs = "INE009A01021", "INE467B01029"
    clash = _bhav_rows(f"{infy},EQ,1500.55", f"{tcs},EQ,3333.10", f"{infy},EQ,3333.10")
    assert [q.isin for q in eod.parse_bhavcopy(clash, TODAY).quotes] == [tcs]
    # a better series settles it, whichever row comes first
    for rows in (
        (f"{infy},BE,1", f"{infy},BE,2", f"{infy},EQ,1500.55"),
        (f"{infy},EQ,1500.55", f"{infy},BE,1", f"{infy},BE,2"),
        (f"{infy},EQ,1500.55", f"{infy},EQ,1500.55"),
    ):
        (q,) = eod.parse_bhavcopy(_bhav_rows(*rows), TODAY).quotes
        assert q.price == Price.rupees("1500.55"), rows


def test_the_block_deal_series_loses_to_any_other() -> None:
    """A real file (30 Sep 2026) has an InvIT in IV and BL. Both were
    "other" series; its own series is the price, not the block window."""
    for rows in (("INE009A01021,BL,63.29", "INE009A01021,IV,64.22"),):
        for order in (rows, rows[::-1]):
            (q,) = eod.parse_bhavcopy(_bhav_rows(*order), TODAY).quotes
            assert q.price == Price.rupees("64.22")
    (q,) = eod.parse_bhavcopy(_bhav_rows("INE009A01021,BL,63.29"), TODAY).quotes
    assert q.price == Price.rupees("63.29")  # only a block price: better than none


def test_today_is_the_day_in_india() -> None:
    """Both files are dated in India. At 19:00 UTC it is already tomorrow
    there, and a file with that date is not "after today"."""
    from datetime import datetime

    from stockin.prices import india_day as _india_day

    assert _india_day(datetime(2026, 11, 17, 18, 29, tzinfo=UTC)) == date(2026, 11, 17)
    assert _india_day(datetime(2026, 11, 17, 18, 30, tzinfo=UTC)) == date(2026, 11, 18)


# ------------------------------------------------------------- jumps (verifier 5)


def test_a_jump_is_more_than_double_or_less_than_half() -> None:
    from stockin.prices import is_jump

    def jump(a: str, b: str) -> bool:
        return is_jump(Price.rupees(a), Price.rupees(b))

    assert not jump("100", "200")  # exactly double: a 1:1 bonus the other way round
    assert jump("100", "200.000001")
    assert not jump("100", "50")
    assert jump("100", "49.999999")
    assert not jump("1367", "1367")
    assert jump("1108.60", "1108260")  # verifier 5: the decimal point became a digit
    assert jump("1367", "9367")  # and a damaged leading digit


def test_hold_jumps_holds_only_new_jumps_and_does_not_ask_twice() -> None:
    from stockin.prices import Jump, hold_jumps

    infy, tcs, bees = "INE009A01021", "INE467B01029", "INF204KB14I2"
    d13, d14 = date(2026, 11, 13), date(2026, 11, 14)
    last = PriceBook.best([_q(infy, d13, "1500", "bhavcopy"), _q(tcs, d13, "3300", "bhavcopy")])
    wild = _q(infy, d14, "1500255", "bhavcopy")
    calm = _q(tcs, d14, "3333.10", "bhavcopy")
    new = _q(bees, d14, "287.61", "bhavcopy")  # no earlier price: nothing to compare with
    file = _file(wild, calm, new)
    kept, held = hold_jumps(file, last, {})
    assert kept.quotes == (calm, new)
    assert held == (Jump(quote=wild, last=last.quotes[parse_isin(infy)]),)
    # another price stored for the day does not stay: it may be from a damaged copy
    was = _q(infy, d14, "1500.55", "bhavcopy")
    kept, held = hold_jumps(file, last, {was.isin: was})
    assert kept.quotes == (calm, new) and len(held) == 1
    # the jump itself is already stored (accepted before): not held again
    kept, held = hold_jumps(file, last, {wild.isin: wild})
    assert kept == file and held == ()


def _damaged_bhav(old: bytes, new: bytes) -> bytes:
    assert BHAV.count(old) == 1
    return BHAV.replace(old, new)


def _held_infy(tmp_path: Path) -> tuple[str, str]:
    view = json.loads(runner.invoke(app, ["holdings", "--json"]).output)
    (infy,) = [v for v in view["valued"] if v["holding"]["isin"] == "INE009A01021"]
    return _text_price(infy["quote"]["price"]["micro_rupees"]), infy["quote"]["on"]


def _text_price(micros: int) -> str:
    return f"{micros // 10**6}.{micros % 10**6:06d}"


def test_a_price_that_jumps_is_held_until_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifier 5: '1108.60' -> '1108260' was stored with exit 0. With an
    earlier price to compare with, such a row is not stored, the run says so
    with both prices and exits 1, and --accept-jumps stores it (a real split
    or bonus must still get in)."""
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    files = {
        "day13.csv": _bhav_for("2026-11-13"),
        "wild.csv": _damaged_bhav(b"INFY LTD,1,1,1,1500.55,", b"INFY LTD,1,1,1,1500255,"),
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    wild = ["prices", "import", str(tmp_path / "wild.csv")]
    assert runner.invoke(app, ["prices", "import", str(tmp_path / "day13.csv")]).exit_code == 0

    result = runner.invoke(app, wild)
    assert result.exit_code == 1, result.output
    assert (
        "NOT stored: INE009A01021 was 1,500.55 on 2026-11-13, "
        "this file says 15,00,255.00 on 2026-11-14"
    ) in result.output
    assert "--accept-jumps" in result.output
    assert _held_infy(tmp_path) == ("1500.550000", "2026-11-13")
    as_json = runner.invoke(app, [*wild, "--json"])
    assert as_json.exit_code == 1
    (held,) = json.loads(as_json.output)["held"]
    assert held["quote"]["price"]["micro_rupees"] == 1_500_255_000_000
    assert held["last"]["on"] == "2026-11-13"

    accepted = runner.invoke(app, [*wild, "--accept-jumps"])
    assert accepted.exit_code == 0, accepted.output
    assert "NOT stored" not in accepted.output
    assert _held_infy(tmp_path) == ("1500255.000000", "2026-11-14")
    again = runner.invoke(app, wild)  # accepted once: the same file is not held again
    assert again.exit_code == 0, again.output
    assert _held_infy(tmp_path) == ("1500255.000000", "2026-11-14")

    right = runner.invoke(app, ["prices", "import", str(EOD / "bhavcopy-sample.csv")])
    assert right.exit_code == 0, right.output
    assert _held_infy(tmp_path) == ("1500.550000", "2026-11-14")


def test_fetch_holds_a_jump_and_takes_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    day13 = tmp_path / "day13.csv"
    day13.write_bytes(_bhav_for("2026-11-13"))
    runner.invoke(app, ["prices", "import", str(day13)])
    wild = eod.parse_bhavcopy(
        _damaged_bhav(b"INFY LTD,1,1,1,1500.55,", b"INFY LTD,1,1,1,500.55,"), TODAY
    )
    monkeypatch.setattr(eod, "fetch_amfi", lambda today: eod.parse_amfi(NAVALL, TODAY))
    monkeypatch.setattr(eod, "fetch_bhavcopy", lambda today: wild)
    result = runner.invoke(app, ["prices", "fetch"])
    assert result.exit_code == 1, result.output
    assert "NOT stored: INE009A01021 was 1,500.55 on 2026-11-13" in result.output
    assert _held_infy(tmp_path) == ("1500.550000", "2026-11-13")
    assert runner.invoke(app, ["prices", "fetch", "--accept-jumps"]).exit_code == 0
    assert _held_infy(tmp_path) == ("500.550000", "2026-11-14")


# --------------------------------- verifier 5's sticky cases, through the commands

_FUND_ROW = "100000;INF179K01BE2;-;Synthetic Fixture Fund;Direct Plan;Growth;N.A.;14-Nov-2026"
_NAV_HELD = NAVALL.replace(_FUND_ROW, _FUND_ROW.replace("N.A.;14-Nov", "77.7;13-Nov"))
"""The fixture NAVAll with a NAV for the fund the fixture ledger holds."""

_STICKY = {
    # name: (clean file, text to change, its replacement, exit code of the damaged import)
    "close-digit": (BHAV, b"INFY LTD,1,1,1,1500.55,", b"INFY LTD,1,1,1,9500.55,", 0),
    "close-1000x": (BHAV, b"INFY LTD,1,1,1,1500.55,", b"INFY LTD,1,1,1,1500255,", 0),
    # the EQ row's ISIN no longer checks: the block-deal row's close is taken instead
    "close-row-lost": (BHAV, b"INE009A01021,INFY,EQ", b"INE009A01022,INFY,EQ", 0),
    "bhav-year-9026": (BHAV, b"2026-11-14,2026-11-14,CM,NSE,STK,1,INE467B01029", b"9", 2),
    "bhav-year-2028": (BHAV, b"2026-11-14,2026-11-14,CM,NSE,STK,1,INE467B01029", b"2028", 2),
    "nav-digit": (_NAV_HELD.encode(), b"77.7;13-Nov", b"97.7;13-Nov", 0),
    "nav-date-later": (_NAV_HELD.encode(), b"77.7;13-Nov", b"77.7;17-Nov", 0),
    "nav-date-2028": (_NAV_HELD.encode(), b"77.7;13-Nov-2026", b"77.7;13-Nov-2028", 2),
    "nav-other-line-isin": (_NAV_HELD.encode(), b"118955;INF179K01UT0", b"118955;INF179K01BE2", 0),
    "nav-row-lost": (_NAV_HELD.encode(), b"77.7;13-Nov", b"N.A.;13-Nov", 0),
}


@pytest.mark.parametrize("name", sorted(_STICKY))
@pytest.mark.parametrize("history", [False, True], ids=["fresh", "after-an-earlier-day"])
def test_importing_the_right_file_undoes_a_damaged_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, history: bool
) -> None:
    """Verifier 5: each of these was stored with exit 0 and stayed after the
    right file was imported (a higher price, a later date, another line's
    ISIN). Now each is rejected, held, or gone once the right file is in:
    holdings and doctor then say exactly what they say without the damage."""
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    monkeypatch.setattr("stockin.cli._today", lambda: TODAY)
    clean, old, new, code = _STICKY[name]
    assert clean.count(old) == 1
    files = {"right": clean, "damaged": clean.replace(old, new + old[len(new) :])}
    if history:
        files["earlier"] = clean.replace(b"2026-11-14", b"2026-11-12").replace(
            b"14-Nov-2026", b"12-Nov-2026"
        )
    for stem, data in files.items():
        (tmp_path / stem).write_bytes(data)

    def state(*imports: str) -> tuple[str, str]:
        for path in tmp_path.glob("prices.db*"):
            path.unlink()
        for stem in ["earlier"] * history + list(imports):
            result = runner.invoke(app, ["prices", "import", str(tmp_path / stem)])
            want = (code, 1) if stem == "damaged" and history else (code,)
            assert result.exit_code in (want if stem == "damaged" else (0,)), result.output
        doctor = json.loads(runner.invoke(app, ["doctor", "--json"]).output)["checks"]
        return runner.invoke(app, ["holdings", "--json"]).output, json.dumps(doctor)

    want = state("right")
    assert '"quote": {' in want[0]  # the right file does price a holding
    if code == 0 and not history:
        assert state("damaged") != want  # the damage did get in
    assert state("damaged", "right") == want
    assert state("right", "damaged", "right") == want


# ------------------------------------- verifier 5's surviving mutants (its N1)


@pytest.mark.parametrize("call", ["book", "stored", "replace", "take", "latest_day", "count"])
def test_a_prices_db_that_changes_under_an_open_store_is_reported(
    tmp_path: Path, call: str
) -> None:
    """Each store method turns SQLite failing mid-use into PricesDamaged.
    Here another program drops the table after the store was opened."""
    import sqlite3

    from stockin.prices import PricesDamaged

    db = tmp_path / "prices.db"
    quote = _q("INE009A01021", date(2026, 11, 14), "1500.55", "bhavcopy")
    other = _file(_q("INE009A01021", date(2026, 11, 14), "1500", "bhavcopy"))
    with PriceStore.open(db, write=True) as store:
        store.replace(_file(quote))
        with sqlite3.connect(db) as elsewhere:
            elsewhere.execute("DROP TABLE quotes")
        calls = {
            "book": lambda: store.book([quote.isin]),
            "stored": lambda: store.stored("bhavcopy", quote.on),
            "replace": lambda: store.replace(other),
            "take": lambda: store.take(other),
            "latest_day": store.latest_day,
            "count": store.count,
        }
        with pytest.raises(PricesDamaged, match=r"not a Stockin price file \(no such table"):
            calls[call]()


@pytest.mark.parametrize("side", ["-journal", "-wal"])
def test_a_prices_db_that_cannot_be_written_is_reported_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    """Opening for writing can fail after the file has passed its checks.
    Here SQLite cannot make its journal because that name is a directory."""
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    (tmp_path / "prices.db").touch()
    (tmp_path / f"prices.db{side}").mkdir()
    result = runner.invoke(app, ["prices", "import", str(EOD / "bhavcopy-sample.csv")])
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    assert "not a Stockin price file" in result.output
    assert (tmp_path / "prices.db").read_bytes() == b""


def test_a_file_that_is_not_text_says_so() -> None:
    with pytest.raises(eod.EodRejected, match="neither an NSE bhavcopy nor an AMFI NAVAll"):
        eod.parse_bhavcopy(b"TradDt,ISIN\n\xff\xfe,x\n", TODAY)


@pytest.mark.parametrize("source", ["nse", "amfi"])
def test_the_price_ceiling_is_exactly_one_crore(source: str) -> None:
    """Verifier 5: the ceiling could be raised tenfold and no test failed."""
    assert eod.MAX_PRICE_MICROS == 10_000_000 * 10**6

    def parse(price: str) -> PriceFile:
        if source == "nse":
            return eod.parse_bhavcopy(_bhav_rows(f"INE009A01021,EQ,{price}"), TODAY)
        row = f"1;INF179K01UT0;-;Fund;Direct Plan;Growth;{price};14-Nov-2026"
        return eod.parse_amfi(_nav(row), TODAY)

    assert parse("10000000.000000").quotes[0].price.micro_rupees == eod.MAX_PRICE_MICROS
    for above in ("10000000.000001", "10000001", "99999999"):
        with pytest.raises(eod.EodRejected, match="beyond any real price"):
            parse(above)


def test_a_navall_row_shorter_than_the_header_is_rejected() -> None:
    """Verifier 5: only longer rows were tested. One short row, and a file
    whose every row is one field short of its header, are both rejected: the
    width comes from the header, never from the rows."""
    good = "1;INF179K01UT0;-;Fund;Direct Plan;Growth;10.5;14-Nov-2026"
    short = "2;INF179K01VY8;-;Fund;Direct Plan;43.5;14-Nov-2026"
    with pytest.raises(eod.EodRejected, match="line 6 has 7 fields but the header has 8"):
        eod.parse_amfi(_nav(good, short), TODAY)
    with pytest.raises(eod.EodRejected, match="line 5 has 7 fields but the header has 8"):
        eod.parse_amfi(_nav(short, short.replace("VY8", "UT0")), TODAY)


def test_an_older_navall_without_plan_and_option_columns_is_read() -> None:
    header = "Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Scheme Name;"
    old = header + "Net Asset Value;Date\n1;INF179K01UT0;-;Fund;10.5;14-Nov-2026\n"
    (q,) = eod.parse_amfi(old, TODAY).quotes
    assert (q.price, q.on) == (Price.rupees("10.5"), date(2026, 11, 14))
    # fewer columns than that is not a NAVAll: the ISINs, NAV and date would overlap
    with pytest.raises(eod.EodRejected, match="not an AMFI NAVAll file"):
        eod.parse_amfi(
            "Scheme Code;ISIN;Name;Net Asset Value;Date\n1;INF179K01UT0;F;10.5;x\n", TODAY
        )


def test_bhavcopy_header_names_may_be_padded() -> None:
    padded = b" TradDt , ISIN ,SctySrs , ClsPric\n2026-11-14,INE009A01021,EQ,1500.55\n"
    (q,) = eod.parse_bhavcopy(padded, TODAY).quotes
    assert q.price == Price.rupees("1500.55")
