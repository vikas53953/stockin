import json
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
