import bisect
import io
import json
import re
import zipfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stockin.cli import app
from stockin.facts import parse_isin
from stockin.money import Money, Price, Qty
from stockin.prices import PriceBook, PriceStore, Quote
from stockin.sources import eod

FIX = Path(__file__).parent / "fixtures"
EOD = FIX / "eod"
runner = CliRunner()


def test_amfi_reads_nav_and_date_and_both_isin_columns() -> None:
    quotes = {q.isin: q for q in eod.parse_amfi((EOD / "NAVAll-sample.txt").read_text())}
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
        eod.parse_amfi("hello;world\n")


@pytest.mark.parametrize("name", ["bhavcopy-sample.csv", "bhavcopy-sample.csv.zip"])
def test_bhavcopy_reads_close_and_prefers_eq_series(name: str) -> None:
    quotes = {q.isin: q for q in eod.parse_bhavcopy((EOD / name).read_bytes())}
    infy = quotes[parse_isin("INE009A01021")]
    assert infy.price == Price.rupees("1500.55")  # EQ, not the higher BL row
    assert infy.on == date(2026, 11, 14)
    assert infy.source == "bhavcopy"
    assert len(quotes) == 4


def test_bhavcopy_rejects_another_csv() -> None:
    with pytest.raises(eod.EodRejected):
        eod.parse_bhavcopy(b"a,b,c\n1,2,3\n")


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


def test_store_is_idempotent_and_reads_on_or_before(tmp_path: Path) -> None:
    db = tmp_path / "prices.db"
    a = _q("INE009A01021", date(2026, 11, 13), "1490", "bhavcopy")
    b = _q("INE009A01021", date(2026, 11, 14), "1500.55", "bhavcopy")
    with PriceStore.open(db, write=True) as store:
        assert store.put([a, b]) == 2
        first = store.digest()
        assert store.put([b, a]) == 0
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
    bhav = eod.parse_bhavcopy((EOD / "bhavcopy-sample.csv").read_bytes())

    def down() -> tuple[Quote, ...]:
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(eod, "fetch_amfi", down)
    monkeypatch.setattr(eod, "fetch_bhavcopy", lambda today: bhav)
    result = runner.invoke(app, ["prices", "fetch", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["kept"] == 1
    assert report["failed"] and report["failed"][0].startswith("AMFI")

    monkeypatch.setattr(eod, "fetch_bhavcopy", lambda today: down())
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
        eod.parse_bhavcopy(data)


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
    amfi = eod.parse_amfi((EOD / "NAVAll-sample.txt").read_text())
    monkeypatch.setattr(eod, "fetch_amfi", lambda: amfi)
    monkeypatch.setattr(eod, "_get", lambda url: _damaged_zip())
    result = runner.invoke(app, ["prices", "fetch", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["read"] == len(amfi) > 0  # the AMFI prices were not thrown away
    assert len(report["failed"]) == 1 and report["failed"][0].startswith("NSE")


def test_an_empty_price_file_is_rejected() -> None:
    with pytest.raises(eod.EodRejected):
        eod.parse_amfi((EOD / "NAVAll-sample.txt").read_text().splitlines()[0] + "\n")
    with pytest.raises(eod.EodRejected):
        eod.parse_bhavcopy(b"TradDt,ISIN,SctySrs,ClsPric\n")


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
        store.put(
            [
                _q("INE009A01021", date(2026, 11, 3), "1400", "bhavcopy"),
                _q("INE009A01021", date(2026, 11, 14), "1500", "bhavcopy"),
            ]
        )
    checks = {
        c["name"]: c for c in json.loads(runner.invoke(app, ["doctor", "--json"]).output)["checks"]
    }
    assert "latest 2026-11-14" in checks["quotes"]["detail"]
    assert "2 end-of-day prices" in checks["quotes"]["detail"]


def test_a_holiday_404_looks_back_and_weekends_are_never_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    data = (EOD / "bhavcopy-sample.csv.zip").read_bytes()

    def get(url: str) -> bytes | None:
        ymd = url.split("_0_0_0_")[1][:8]
        asked.append(ymd)
        return data if ymd == "20261112" else None  # Thu has a file; Fri a holiday

    monkeypatch.setattr(eod, "_get", get)
    # Monday 16 Nov: Mon 404, Sun and Sat skipped, Fri 404, Thu found.
    assert eod.fetch_bhavcopy(date(2026, 11, 16))
    assert asked == ["20261116", "20261113", "20261112"]


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
    amfi = eod.parse_amfi((EOD / "NAVAll-sample.txt").read_text())

    def cut(req: object, timeout: float) -> _Resp:
        raise http.client.IncompleteRead(b"PK", 9000)

    monkeypatch.setattr(eod, "fetch_amfi", lambda: amfi)
    monkeypatch.setattr(eod.urllib.request, "urlopen", cut)
    monkeypatch.setattr(eod, "_sleep", lambda s: None)
    result = runner.invoke(app, ["prices", "fetch", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["read"] == len(amfi)
    assert len(report["failed"]) == 1 and report["failed"][0].startswith("NSE")
    assert "unexpected" not in report["failed"][0]  # a known failure, said plainly


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
    data = (EOD / "bhavcopy-sample.csv.zip").read_bytes()

    def get(url: str) -> bytes | None:
        return data if "_20261109_" in url else None  # only the Monday a week ago

    monkeypatch.setattr(eod, "_get", get)
    assert eod.fetch_bhavcopy(date(2026, 11, 16))


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
        eod.parse_bhavcopy(_zip_edits()[name])


def test_every_single_byte_flip_of_a_bhavcopy_zip_parses_or_is_rejected() -> None:
    whole = (EOD / "bhavcopy-sample.csv.zip").read_bytes()
    good = eod.parse_bhavcopy(whole)
    for i in range(len(whole)):
        for bit in (0x01, 0x80):
            data = bytearray(whole)
            data[i] ^= bit
            try:
                eod.parse_bhavcopy(bytes(data))
            except eod.EodRejected:
                continue
            # accepted: only bytes the zip format ignores (times, comments) may change
            assert eod.parse_bhavcopy(bytes(data)) == good, (i, bit)


def test_a_csv_cut_off_mid_row_is_rejected() -> None:
    text = (EOD / "bhavcopy-sample.csv").read_bytes()
    with pytest.raises(eod.EodRejected, match="mid-row"):
        eod.parse_bhavcopy(text[:-7])


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
    amfi = eod.parse_amfi((EOD / "NAVAll-sample.txt").read_text())

    def boom(today: date) -> tuple[Quote, ...]:
        raise KeyError("something nobody planned for")

    monkeypatch.setattr(eod, "fetch_amfi", lambda: amfi)
    monkeypatch.setattr(eod, "fetch_bhavcopy", boom)
    result = runner.invoke(app, ["prices", "fetch", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["read"] == len(amfi)
    assert report["failed"] == ["NSE: unexpected KeyError: 'something nobody planned for'"]


def _isins_on(line: bytes) -> set[str]:
    return {m.decode("latin-1") for m in re.findall(rb"[A-Z]{2}[A-Z0-9]{9}[0-9]", line)}


def _only_that_line_changed(
    good: tuple[Quote, ...], got: tuple[Quote, ...], before: bytes, after: bytes
) -> bool:
    """A one-byte change may alter what its own line says, nothing else: every
    quote for an ISIN that is not on the damaged line must be exactly the
    good one, and a damaged header line may change nothing at all."""
    touched = _isins_on(before) | _isins_on(after)
    a = {q.isin: q for q in good if q.isin not in touched}
    b = {q.isin: q for q in got if q.isin not in touched}
    return a == b


def _one_byte_damage(whole: bytes) -> Iterator[tuple[int, bytes, bytes, bytes]]:
    """(byte, damaged file, its line before, its line after) for every byte
    and four kinds of change: CR, quote, NUL and one flipped bit."""
    starts = [0, *(i + 1 for i, c in enumerate(whole) if c == 0x0A)]
    for i in range(len(whole)):
        line_no = bisect.bisect_right(starts, i) - 1
        lo = starts[line_no]
        # a damaged line break joins this line with the next: both are touched
        hi = whole.find(b"\n", i + 1 if whole[i] == 0x0A else lo)
        hi = len(whole) if hi < 0 else hi
        for new in (0x0D, 0x22, 0x00, whole[i] ^ 0x01):
            data = bytearray(whole)
            data[i] = new
            yield i, bytes(data), whole[lo:hi], bytes(data[lo:hi])


def test_every_single_byte_change_of_a_bhavcopy_csv_is_rejected_or_stays_on_its_line() -> None:
    """Verifier 4: a comma lost from the header shifted every column, and the
    day's low was stored as the close with exit 0. The header and each row
    must agree on the number of fields, so a shifted column is rejected."""
    whole = (EOD / "bhavcopy-sample.csv").read_bytes()
    good = eod.parse_bhavcopy(whole)
    for i, data, before, after in _one_byte_damage(whole):
        try:
            got = eod.parse_bhavcopy(data)
        except eod.EodRejected:
            continue
        assert _only_that_line_changed(good, got, before, after), (i, after)


def test_every_single_byte_change_of_a_navall_is_rejected_or_stays_on_its_line() -> None:
    """Lesson 18's rule for the AMFI file too: decoded the way fetch decodes
    it, every damaged copy parses or is rejected, and a change reaches only
    the line it is on."""
    whole = (EOD / "NAVAll-sample.txt").read_bytes()
    good = eod.parse_amfi(whole.decode("utf-8-sig"))
    for i, data, before, after in _one_byte_damage(whole):
        try:
            got = eod.parse_amfi(data.decode("utf-8-sig", errors="replace"))
        except eod.EodRejected:
            continue
        assert _only_that_line_changed(good, got, before, after), (i, after)


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
        eod.parse_bhavcopy(whole.replace(old, new, 1))


def test_a_stray_quote_cannot_swallow_the_rows_after_it() -> None:
    whole = (EOD / "bhavcopy-sample.csv").read_bytes()
    first_row_end = whole.index(b"\n", whole.index(b"\n") + 1)
    damaged = whole[:first_row_end] + b'"' + whole[first_row_end + 1 :]
    with pytest.raises(eod.EodRejected):
        eod.parse_bhavcopy(damaged)


def test_a_quoted_field_spanning_rows_is_rejected() -> None:
    """Two stray quotes can wrap a whole row into one field and keep the
    field count right; no NSE field spans lines."""
    whole = (EOD / "bhavcopy-sample.csv").read_bytes()
    lines = whole.split(b"\n")
    lines[1] = lines[1][:-1] + b'"'  # the last (empty) field opens a quote...
    lines[2] = lines[2] + b'"'  # ...that closes at the end of the next row
    with pytest.raises(eod.EodRejected, match="runs over several lines"):
        eod.parse_bhavcopy(b"\n".join(lines))


def test_text_after_a_closing_quote_is_rejected_not_glued_on() -> None:
    """The lenient CSV reader would read "1500.55"5 as 1500.555."""
    whole = (EOD / "bhavcopy-sample.csv").read_bytes()
    damaged = whole.replace(b",1500.55,1500.55,", b',"1500.55"5,1500.55,', 1)
    assert damaged != whole
    with pytest.raises(eod.EodRejected, match="damaged"):
        eod.parse_bhavcopy(damaged)


def test_two_navall_rows_run_together_are_rejected() -> None:
    text = (EOD / "NAVAll-sample.txt").read_text()
    joined = text.replace("14-Nov-2026\n118969", "14-Nov-2026\x00118969", 1)
    assert joined != text
    with pytest.raises(eod.EodRejected, match="fields but the header has"):
        eod.parse_amfi(joined)


def test_columns_are_found_by_name_in_any_order() -> None:
    """Verifier 4's crafted file: TradDt last, then a short row. Columns in
    any order parse; the short row is rejected, not an AttributeError."""
    good = b"ISIN,ClsPric,SctySrs,TradDt\nINE009A01021,1500.55,EQ,2026-11-14\n"
    (q,) = eod.parse_bhavcopy(good)
    assert (q.isin, q.price, q.on) == ("INE009A01021", Price.rupees("1500.55"), date(2026, 11, 14))
    with pytest.raises(eod.EodRejected, match="row 3 has 2 fields"):
        eod.parse_bhavcopy(good + b"INE009A01021,1500.55\n")
    with pytest.raises(eod.EodRejected, match="names a column twice"):
        eod.parse_bhavcopy(b"ISIN,ClsPric,SctySrs,TradDt,ISIN\nA,1,EQ,2026-11-14,B\n")


@pytest.mark.parametrize("source", ["nse", "amfi"])
def test_a_price_beyond_any_real_price_rejects_the_file(source: str) -> None:
    """Verifier 4: a 14-digit close crashed SQLite with OverflowError. ₹1
    crore a unit is the ceiling; one cell above it rejects the file."""
    huge = "12345678901234.5"
    if source == "nse":
        data = b"TradDt,ISIN,SctySrs,ClsPric\n2026-11-14,INE009A01021,EQ," + huge.encode() + b"\n"
        with pytest.raises(eod.EodRejected, match="beyond any real price"):
            eod.parse_bhavcopy(data)
    else:
        text = (EOD / "NAVAll-sample.txt").read_text().replace("2100.1234", huge)
        with pytest.raises(eod.EodRejected, match="beyond any real price"):
            eod.parse_amfi(text)
    edge = f"{eod.MAX_PRICE_MICROS // 10**6}"
    row = b"TradDt,ISIN,SctySrs,ClsPric\n2026-11-14,INE009A01021,EQ," + edge.encode() + b"\n"
    assert eod.parse_bhavcopy(row)  # exactly the ceiling is still a price
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
    bad = b"TradDt,ISIN,SctySrs,ClsPric\n2026-11-14,INE009A01021,EQ,99999999999999\n"
    monkeypatch.setattr(eod, "_get", lambda url: navall if "amfi" in url else bad)
    result = runner.invoke(app, ["prices", "fetch", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["read"] == len(eod.parse_amfi(navall.decode()))  # AMFI's prices survived
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
        eod.parse_bhavcopy(data)


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
    assert eod.fetch_amfi() == eod.parse_amfi((EOD / "NAVAll-sample.txt").read_text())


def _store_with_rows(path: Path, n: int = 400) -> None:
    """A real prices.db, big enough to span several pages."""
    with PriceStore.open(path, write=True) as store:
        store.put(
            _q(_with_digit(f"INE{i:08d}"), date(2026, 11, 14), "100", "bhavcopy") for i in range(n)
        )


def _with_digit(body: str) -> str:
    for d in "0123456789":
        try:
            return parse_isin(body + d)
        except ValueError:
            continue
    raise AssertionError(body)


def _damage(kind: str, db: Path) -> None:
    import sqlite3

    if kind == "second-page":
        _store_with_rows(db)
        data = bytearray(db.read_bytes())
        page = int.from_bytes(data[16:18], "big")
        data[page : page * 2] = b"\xab" * page  # page 2 overwritten
        db.write_bytes(bytes(data))
    elif kind in ("text-price", "bad-date", "unknown-source", "no-primary-key"):
        with sqlite3.connect(db) as c:
            pk = "" if kind == "no-primary-key" else ", PRIMARY KEY(isin, on_date, source)"
            c.execute(f"CREATE TABLE quotes(isin, on_date, source, micro_rupees{pk})")
            row = {
                "text-price": ("INE009A01021", "2026-11-14", "bhavcopy", "lots"),
                "bad-date": ("INE009A01021", "14/11/2026", "bhavcopy", 1),
                "unknown-source": ("INE009A01021", "2026-11-14", "broker", 1),
                "no-primary-key": ("INE009A01021", "2026-11-14", "bhavcopy", 1),
            }[kind]
            c.execute("INSERT INTO quotes VALUES (?,?,?,?)", row)
    elif kind == "directory":
        db.mkdir()


@pytest.mark.parametrize(
    "kind",
    ["second-page", "text-price", "bad-date", "unknown-source", "no-primary-key", "directory"],
)
def test_a_damaged_or_foreign_prices_db_is_reported_and_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Verifier 4: a damaged second page crashed doctor, holdings and import
    with DatabaseError (and import rewrote the file); a foreign file with our
    column names passed the shape check; a directory crashed import."""
    _ledger_with_mf_and_demat(tmp_path, monkeypatch)
    db = tmp_path / "prices.db"
    _damage(kind, db)
    before = None if kind == "directory" else db.read_bytes()
    commands = [["prices", "import", str(EOD / "bhavcopy-sample.csv")]]
    if kind != "directory":  # a directory is not a price file to read: holdings run unpriced
        commands.append(["holdings"])
    checks = json.loads(runner.invoke(app, ["doctor", "--json"]).output)["checks"]
    (quotes,) = [c for c in checks if c["name"] == "quotes"]
    assert quotes["status"] == "fail", quotes
    for args in commands:
        result = runner.invoke(app, args)
        assert result.exit_code == 2, (args, result.output)
        assert result.exception is None or isinstance(result.exception, SystemExit), result.output
        assert "move prices.db aside" in result.output
    if before is not None:
        assert db.read_bytes() == before
