"""Live share prices: the parsers on NSE- and Yahoo-shaped bytes, exact
paise from JSON numbers (as a property), when a quote counts as live, how a
live quote ranks against closes, and `prices live` / `watch --once`."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
import typer
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

import stockin.cli as cli_mod
import stockin.prices as prices_mod
from stockin.cli import app as cli
from stockin.facts import Isin, parse_isin
from stockin.money import Price
from stockin.prices import PriceBook, PriceFile, PriceStore, Quote
from stockin.sources import live
from stockin.sources.live import IST, LiveQuote, LiveRejected, as_quote, market_open

FIX = Path(__file__).parent / "fixtures"
INFY = parse_isin("INE009A01021")
HDFC = parse_isin("INF179K01BE2")
runner = CliRunner()
MON = datetime(2026, 11, 16, 13, 20, tzinfo=IST)  # a Monday, market open


def chart(symbol: str, price: str, at: datetime, currency: str = "INR") -> bytes:
    meta = (
        f'{{"currency":"{currency}","symbol":"{symbol}","regularMarketPrice":{price},'
        f'"regularMarketTime":{int(at.timestamp())},"exchangeTimezoneName":"Asia/Kolkata"}}'
    )
    return f'{{"chart":{{"result":[{{"meta":{meta}}}],"error":null}}}}'.encode()


# ------------------------------------------------------------ parsers


def test_symbols_from_nse_list() -> None:
    found = live.parse_symbols((FIX / "live" / "EQUITY_L-sample.csv").read_bytes())
    assert found[INFY] == "INFY" and found[parse_isin("INE101A01026")] == "M&M"
    assert len(found) == 4  # the bad ISIN row is skipped


def test_symbols_drop_an_isin_listed_twice_and_refuse_other_files() -> None:
    data = b"SYMBOL, ISIN NUMBER\nINFY,INE009A01021\nINFYX,INE009A01021\nTCS,INE467B01029\n"
    assert live.parse_symbols(data) == {parse_isin("INE467B01029"): "TCS"}
    with pytest.raises(LiveRejected):
        live.parse_symbols(b"<html>Access denied</html>")
    with pytest.raises(LiveRejected):
        live.parse_symbols(b"SYMBOL,ISIN NUMBER\n")


def test_chart_reads_price_to_the_paise_and_time_in_india() -> None:
    q = live.parse_chart((FIX / "live" / "chart-INFY.json").read_bytes(), INFY, "INFY")
    assert q.price == Price.rupees("1523.60")  # 1523.5999755859375 -> half-even paise
    assert q.at == datetime(2025, 11, 17, 13, 15, tzinfo=IST)


@pytest.mark.parametrize(
    "data",
    [
        chart("TCS.NS", "10", MON),
        chart("INFY.NS", "10", MON, currency="USD"),
        chart("INFY.NS", "0", MON),
        chart("INFY.NS", "-5", MON),
        chart("INFY.NS", "1e30", MON),
        chart("INFY.NS", '"10"', MON),
        b'{"chart":{"result":[],"error":{"code":"Not Found"}}}',
        b'{"chart":{"result":[{"meta":{"symbol":"INFY.NS","currency":"INR",'
        b'"regularMarketPrice":10,"regularMarketTime":1.5}}]}}',
        b'{"chart":{"result":[{"meta":{"symbol":"INFY.NS","currency":"INR",'
        b'"regularMarketPrice":10,"regularMarketTime":99999999999999999}}]}}',
        b"Too Many Requests",
        b"",
    ],
)
def test_chart_refuses_what_is_not_this_shares_price(data: bytes) -> None:
    with pytest.raises(LiveRejected):
        live.parse_chart(data, INFY, "INFY")


@given(st.decimals(min_value=Decimal(0), max_value=Decimal(10**6), places=12))
def test_a_json_number_becomes_exact_half_even_paise(rupees: Decimal) -> None:
    want = round(Fraction(rupees) * 100)  # round() on a Fraction is half-even
    data = chart("INFY.NS", str(rupees), MON)
    if want == 0:  # under half a paisa is no price at all
        with pytest.raises(LiveRejected):
            live.parse_chart(data, INFY, "INFY")
        return
    assert live.parse_chart(data, INFY, "INFY").price.micro_rupees == want * 10_000


@pytest.mark.parametrize(
    ("raw", "paise"), [("1500.125", 150012), ("1500.135", 150014), ("0.015", 2), ("7.005", 700)]
)
def test_exact_half_paise_round_to_even(raw: str, paise: int) -> None:
    """Random decimals almost never land on an exact half paisa."""
    q = live.parse_chart(chart("INFY.NS", raw, MON), INFY, "INFY")
    assert q.price.micro_rupees == paise * 10_000


# ------------------------------------------------------------ when it is live


def lq(at: datetime) -> LiveQuote:
    return LiveQuote(isin=INFY, symbol="INFY", price=Price.rupees("1500"), at=at)


def test_market_hours() -> None:
    assert market_open(MON)
    assert not market_open(MON.replace(hour=9, minute=14))
    assert market_open(MON.replace(hour=15, minute=30))
    assert not market_open(MON.replace(hour=15, minute=31))
    assert not market_open(MON + timedelta(days=5))  # Saturday


def test_only_a_fresh_trade_from_today_is_live() -> None:
    assert as_quote(lq(MON - timedelta(minutes=5)), MON) == Quote(
        isin=INFY, on=MON.date(), price=Price.rupees("1500"), source="live"
    )
    assert as_quote(lq(MON - timedelta(minutes=21)), MON) is None  # late while open
    assert as_quote(lq(MON - timedelta(days=1)), MON) is None  # a holiday's quote
    assert as_quote(lq(MON + timedelta(minutes=10)), MON) is None  # from the future
    evening = MON.replace(hour=19)
    assert as_quote(lq(MON.replace(hour=15, minute=29)), evening) is not None  # today's last
    friday_close = MON.replace(hour=15, minute=29) - timedelta(days=3)
    assert as_quote(lq(friday_close), evening) is None  # closed, but not today's trade


@given(st.lists(st.sampled_from(["live", "bhavcopy", "amfi"]), max_size=3), st.randoms())
def test_live_beats_a_close_only_on_its_own_day_in_any_order(
    sources: list[str], rnd: object
) -> None:
    day = date(2026, 11, 16)
    quotes = [
        Quote(isin=INFY, on=day, price=Price.rupees(str(10 + n)), source=s)  # type: ignore[arg-type]
        for n, s in enumerate(sources)
    ]
    quotes.append(
        Quote(isin=INFY, on=day - timedelta(days=3), price=Price.rupees("1"), source="live")
    )
    shuffled = list(quotes)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    best = PriceBook.best(shuffled).get(INFY)
    assert best == PriceBook.best(quotes).get(INFY)
    assert best is not None
    if sources:
        rank = {"live": 0, "bhavcopy": 1, "amfi": 2}
        assert best.on == day and best.source == min(sources, key=rank.__getitem__)


def test_live_prices_are_never_stored(tmp_path: Path) -> None:
    file = PriceFile(
        source="live",
        day=date(2026, 11, 16),
        quotes=(Quote(isin=INFY, on=date(2026, 11, 16), price=Price.rupees("1"), source="live"),),
    )
    with PriceStore.open(tmp_path / "prices.db", write=True) as store, pytest.raises(ValueError):
        store.replace(file)


# ------------------------------------------------------------ the CLI


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    for var in ("STOCKIN_TELEGRAM_TOKEN", "STOCKIN_TELEGRAM_CHAT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(prices_mod, "_now", lambda: MON)
    for name in ("mf-2026-01.json", "demat-2026-02.json"):
        assert runner.invoke(cli, ["import", "cas", str(FIX / "cas" / name)]).exit_code == 0
    r = runner.invoke(cli, ["prices", "import", str(FIX / "eod" / "bhavcopy-sample.csv")])
    assert r.exit_code in (0, 1), r.output
    return tmp_path


class Net:
    """Stands in for the two public sources; records what was asked."""

    def __init__(self, price: str = "1650.00", age: timedelta = timedelta(minutes=3)) -> None:
        self.price, self.age, self.asked, self.lists = price, age, [], 0
        self.down = False

    def symbols(self) -> dict[Isin, str]:
        self.lists += 1
        if self.down:
            raise LiveRejected("could not download NSE's list of symbols")
        return live.parse_symbols((FIX / "live" / "EQUITY_L-sample.csv").read_bytes())

    def quote(self, isin: Isin, symbol: str) -> LiveQuote:
        self.asked.append(symbol)
        return live.parse_chart(chart(f"{symbol}.NS", self.price, MON - self.age), isin, symbol)


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Net:
    n = Net()
    monkeypatch.setattr(live, "fetch_symbols", n.symbols)
    monkeypatch.setattr(live, "fetch_quote", n.quote)
    return n


def test_prices_live_overlays_shares_only(data: Path, net: Net) -> None:
    r = runner.invoke(cli, ["prices", "live", "--json"])
    assert r.exit_code == 0, r.output
    view = json.loads(r.output)
    assert net.asked == ["INFY"]  # the fund is never asked for
    (q,) = view["live"]
    assert q["isin"] == INFY and q["price"]["micro_rupees"] == 1_650_000_000
    assert view["market_open"] is True and view["end_of_day"] == []
    runner.invoke(cli, ["prices", "live"])
    assert net.lists == 1  # the symbol list is kept for the day
    human = runner.invoke(cli, ["prices", "live"]).output
    assert "LIVE  INFY" in human and "₹1,650.00" in human


def test_a_late_quote_keeps_the_close_and_says_why(data: Path, net: Net) -> None:
    net.age = timedelta(hours=2)
    view = json.loads(runner.invoke(cli, ["prices", "live", "--json"]).output)
    assert view["live"] == [] and "is not live" in view["end_of_day"][0]["reason"]


def test_no_symbol_list_keeps_every_close(data: Path, net: Net) -> None:
    net.down = True
    r = runner.invoke(cli, ["prices", "live", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.output)["end_of_day"][0]["isin"] == INFY


def test_watch_once_alerts_on_a_live_move_and_not_again(data: Path, net: Net) -> None:
    r = runner.invoke(cli, ["thesis", "set", INFY, "--why", "cash machine", "--target", "95"])
    assert r.exit_code == 0
    # with the close, Infosys is all of the priced money: hold within 5 points of 95%
    r = runner.invoke(cli, ["watch", "--once"])
    assert r.exit_code == 0, r.output
    assert "1 live, 0 at close, 0 new alerts" in r.output
    r = runner.invoke(cli, ["thesis", "broken", INFY])
    r = runner.invoke(cli, ["watch", "--once"])
    assert "1 new alert" in r.output and "EXIT" in r.output
    r = runner.invoke(cli, ["watch", "--once"])
    assert "0 new alerts" in r.output


def test_watch_waits_while_closed_and_stops_on_ctrl_c(
    data: Path, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    saturday = MON + timedelta(days=5)
    monkeypatch.setattr(cli_mod, "india_now", lambda: saturday)
    slept: list[int] = []

    def sleep(s: int) -> None:
        slept.append(s)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "_sleep", sleep)
    r = runner.invoke(cli, ["watch"])
    assert r.exit_code == 0 and "market closed" in r.output and "stopped" in r.output
    assert net.asked == [] and slept == [3600]


def test_until_open_is_the_next_weekday_morning_at_most_an_hour() -> None:
    friday_evening = datetime(2026, 11, 20, 16, 0, tzinfo=IST)
    assert cli_mod._until_open(friday_evening) == 3600  # pyright: ignore[reportPrivateUsage]
    sunday_late = datetime(2026, 11, 22, 23, 0, tzinfo=IST)
    monday_early = datetime(2026, 11, 23, 9, 0, tzinfo=IST)
    assert cli_mod._until_open(sunday_late) == 3600  # pyright: ignore[reportPrivateUsage]
    assert cli_mod._until_open(monday_early) == 15 * 60  # pyright: ignore[reportPrivateUsage]


def test_watch_keeps_going_after_a_failed_pass(
    data: Path, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy alerts.db or any said error in one pass must not stop a
    watch meant to run all day."""
    passes: list[int] = []

    def flaky(send: bool) -> None:
        passes.append(1)
        if len(passes) == 1:
            raise typer.Exit(2)

    slept: list[int] = []

    def sleep(s: int) -> None:
        slept.append(s)
        if len(slept) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "india_now", lambda: MON)
    monkeypatch.setattr(cli_mod, "_watch_pass", flaky)
    monkeypatch.setattr(cli_mod, "_sleep", sleep)
    r = runner.invoke(cli, ["watch"])
    assert r.exit_code == 0 and len(passes) == 2 and slept == [300, 300]
