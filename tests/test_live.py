"""Live share prices: the parsers on NSE- and Yahoo-shaped bytes, exact
paise from JSON numbers (as a property), when a quote counts as live, how a
live quote ranks against closes, and `prices live` / `watch --once`."""

from __future__ import annotations

import json
from collections.abc import Sequence
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
from stockin.prices import PriceBook, PriceFile, PriceStore, Quote, beyond_band
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
def test_on_one_day_the_close_beats_live_beats_nav_in_any_order(
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
        rank = {"bhavcopy": 0, "live": 1, "amfi": 2}  # the official close wins its day
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
        self.now = MON

    def symbols(self) -> tuple[dict[Isin, str], bool]:
        self.lists += 1
        if self.down:
            raise LiveRejected("could not download NSE's list of symbols")
        return live.parse_symbols((FIX / "live" / "EQUITY_L-sample.csv").read_bytes()), True

    def quote(self, isin: Isin, symbol: str) -> LiveQuote:
        self.asked.append(symbol)
        return live.parse_chart(
            chart(f"{symbol}.NS", self.price, self.now - self.age), isin, symbol
        )


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


def test_close_lines_are_in_isin_order(data: Path, net: Net, tmp_path: Path) -> None:
    """V3-5 survivor: a share settled by today's close is found before the
    rest, and the lines still come out by ISIN."""
    assert (
        runner.invoke(
            cli, ["import", "cas", str(FIX / "cas" / "demat-two-accounts.json")]
        ).exit_code
        == 0
    )
    header = (FIX / "eod" / "bhavcopy-sample.csv").read_text().splitlines()[0]
    row = (
        "2026-11-16,2026-11-16,CM,NSE,STK,1,INE062A01020,SBIN,EQ,,,,,SBI,1,1,1,"
        "812.40,812.40,1,,812.40,,,1,1,1,F1,1,,,,,"
    )
    today = tmp_path / "bhav-today.csv"
    today.write_text(f"{header}\n{row}\n")
    r = runner.invoke(cli, ["prices", "import", str(today), "--accept-jumps"])
    assert r.exit_code == 0, r.output
    net.down = True
    view = json.loads(runner.invoke(cli, ["prices", "live", "--json"]).output)
    isins = [f["isin"] for f in view["end_of_day"]]
    assert isins == sorted(isins) == [INFY, "INE062A01020"]
    assert "official close" in view["end_of_day"][1]["reason"]


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


# ------------------------------------------------------------ #33 verifier notes


def _truncating_server(body: bytes, length: int) -> tuple[str, object]:
    """A server that promises `length` bytes and sends `body` then hangs up."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % length + body)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{srv.getsockname()[1]}", srv


@pytest.mark.parametrize(
    ("body", "length"),
    [(b'{"chart":{', 5000), (b"[" * 100_000 + b"]" * 100_000, 200_000)],
)
def test_a_broken_http_answer_falls_back_never_crashes(
    monkeypatch: pytest.MonkeyPatch, body: bytes, length: int
) -> None:
    """R-1: cut short (IncompleteRead) or nested past the parser's depth."""
    base, srv = _truncating_server(body, length)
    monkeypatch.setattr(live, "CHART_URL", base + "/{symbol}")
    try:
        with pytest.raises(LiveRejected):
            live.fetch_quote(INFY, "INFY")
    finally:
        srv.close()  # type: ignore[attr-defined]


def test_a_garbled_status_line_on_the_symbol_list_is_skipped() -> None:
    import http.client

    def get(url: str) -> bytes | None:
        if url == live.EQUITY_LIST_URL:
            raise http.client.BadStatusLine("garbage")
        return b"SYMBOL,ISIN NUMBER\nNIFTYBEES,INF204KB14I2\n"

    found, complete = live.fetch_symbols(get)
    assert found == {parse_isin("INF204KB14I2"): "NIFTYBEES"} and complete is False


def test_a_partial_symbol_list_is_used_and_tried_again_after_an_hour(
    data: Path, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-3 and verify-3 C-4: the equity list failed once; a check an hour
    later tries again."""
    calls: list[int] = []

    def partial() -> tuple[dict[Isin, str], bool]:
        calls.append(1)
        return (
            ({parse_isin("INF204KB14I2"): "NIFTYBEES"}, False) if len(calls) == 1 else net.symbols()
        )

    monkeypatch.setattr(live, "fetch_symbols", partial)
    first = json.loads(runner.invoke(cli, ["prices", "live", "--json"]).output)
    reason = first["end_of_day"][0]["reason"]
    # round 2, R-2: INFY is listed; the list was cut short, and the reason says that
    assert "not listed" not in reason and "incomplete" in reason
    monkeypatch.setattr(cli_mod, "india_now", lambda: MON + timedelta(minutes=61))
    second = json.loads(runner.invoke(cli, ["prices", "live", "--json"]).output)
    # the fake quote is an hour old by now, so INFY stays at close for that reason
    assert len(calls) == 2 and "incomplete" not in json.dumps(second)


def test_the_symbol_list_is_downloaded_again_on_a_new_day(
    data: Path, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V4 survivor: the cache is for one day."""
    runner.invoke(cli, ["prices", "live"])
    runner.invoke(cli, ["prices", "live"])
    assert net.lists == 1
    monkeypatch.setattr(cli_mod, "india_now", lambda: MON + timedelta(days=1))
    runner.invoke(cli, ["prices", "live"])
    assert net.lists == 2


# close ₹1,500.55 (2026-11-14): 20% of it is exactly ₹300.11
HELD = ["750.25", "750.30", "3001.00", "3001.20", "15005.50", "1800.67", "1200.43"]
USED = ["1800.66", "1200.44", "1500.55", "1650.00", "1350.00"]


@pytest.mark.parametrize("price", HELD)
def test_a_live_price_beyond_the_band_keeps_the_close(data: Path, net: Net, price: str) -> None:
    """Round 1 R-2 and round 2 R-1: a split or bonus day must not drive a
    verdict. ₹750.30 is a 1:1 split that opened a paisa up, which the 2x rule
    let through. The last two are one paisa past 20% up and down."""
    net.price = price
    view = json.loads(runner.invoke(cli, ["prices", "live", "--json"]).output)
    assert view["live"] == []
    assert "more than 20% from the close" in view["end_of_day"][0]["reason"]


@pytest.mark.parametrize("price", USED)
def test_a_live_price_within_the_band_is_used(data: Path, net: Net, price: str) -> None:
    """Exactly 20% up or down is a day NSE's widest band allows."""
    net.price = price
    view = json.loads(runner.invoke(cli, ["prices", "live", "--json"]).output)
    assert view["end_of_day"] == []
    assert view["live"][0]["price"] == Price.rupees(price).model_dump(mode="json")


def test_a_split_day_that_opens_up_makes_no_alert(data: Path, net: Net) -> None:
    """Round 2 R-1 on `watch`: the held price is not what verdicts see."""
    net.price = "750.30"
    r = runner.invoke(cli, ["watch", "--once"])
    assert r.exit_code == 0, r.output
    assert "0 live, 1 at close, 0 new alerts" in r.output
    book, _ = cli_mod._live_pass(_book())  # pyright: ignore[reportPrivateUsage]
    assert book.get(INFY) == Quote(
        isin=INFY, on=date(2026, 11, 14), price=Price.rupees("1500.55"), source="bhavcopy"
    )


def _book() -> cli_mod.Book:
    with cli_mod._open_ledger() as ledger:  # pyright: ignore[reportPrivateUsage]
        return ledger.book()


def test_the_reason_names_the_quote_and_the_close_the_right_way_round(data: Path, net: Net) -> None:
    """V2-7 survivor."""
    net.price = "750.30"
    view = json.loads(runner.invoke(cli, ["prices", "live", "--json"]).output)
    reason = view["end_of_day"][0]["reason"]
    assert "live ₹750.30" in reason and "close of ₹1,500.55 on 2026-11-14" in reason


@given(
    st.integers(min_value=1, max_value=10**13),
    st.integers(min_value=1, max_value=10**13),
)
def test_the_band_is_exactly_20_percent_of_the_close_both_ways(close: int, new: int) -> None:
    """Whole micro-rupees, no float: beyond the band means strictly more
    than a fifth of the close away, up or down."""
    want = Fraction(abs(new - close), close) > Fraction(1, 5)
    assert beyond_band(Price(micro_rupees=close), Price(micro_rupees=new)) is want


@pytest.mark.parametrize("close", [1, 5, 10_000, 1_500_550_000, 10**13])
def test_the_band_at_its_edges(close: int) -> None:
    """Random pairs almost never land on the edge itself."""
    c = Price(micro_rupees=close)
    up, down = close + close // 5, close - close // 5
    assert not beyond_band(c, Price(micro_rupees=up))
    assert beyond_band(c, Price(micro_rupees=up + 1))
    if close % 5 == 0:
        assert not beyond_band(c, Price(micro_rupees=down))
        if down > 1:
            assert beyond_band(c, Price(micro_rupees=down - 1))


@pytest.mark.parametrize(("days", "used"), [(7, True), (8, False), (120, False)])
def test_a_live_price_is_checked_only_against_a_recent_close(
    data: Path, net: Net, monkeypatch: pytest.MonkeyPatch, days: int, used: bool
) -> None:
    """Round 2 C-3: a share that really doubled since a months-old close is
    not called a split. An old close cannot check a live price at all, and
    the reason says so and what to run."""
    net.now = datetime(2026, 11, 14, 13, 20, tzinfo=IST) + timedelta(days=days)
    monkeypatch.setattr(prices_mod, "_now", lambda: net.now)
    net.price = "1650.00" if used else "3100.00"
    view = json.loads(runner.invoke(cli, ["prices", "live", "--json"]).output)
    if used:
        assert view["live"][0]["isin"] == INFY and view["end_of_day"] == []
        return
    reason = view["end_of_day"][0]["reason"]
    assert view["live"] == [] and "split" not in reason
    assert "2026-11-14" in reason and "too old" in reason and "stockin prices fetch" in reason


def test_a_live_price_with_no_close_to_check_it_against_is_not_used(data: Path, net: Net) -> None:
    (data / "prices.db").unlink()
    view = json.loads(runner.invoke(cli, ["prices", "live", "--json"]).output)
    reason = view["end_of_day"][0]["reason"]
    assert view["live"] == [] and "no end-of-day price" in reason


# ------------------------------------------------------------ #33 verifier, round 2


def test_a_list_without_etfs_is_not_complete() -> None:
    """Round 2 R-2: the mirror of round 1's R-3."""
    equity = (FIX / "live" / "EQUITY_L-sample.csv").read_bytes()

    def get(url: str) -> bytes | None:
        return equity if url == live.EQUITY_LIST_URL else None  # the ETF list: 404

    found, complete = live.fetch_symbols(get)
    assert found[INFY] == "INFY" and complete is False

    def both(url: str) -> bytes | None:
        return equity if url == live.EQUITY_LIST_URL else ETF_LIST

    found, complete = live.fetch_symbols(both)
    assert found[NIFTYBEES] == "NIFTYBEES" and found[INFY] == "INFY" and complete is True


ETF_LIST = b"Symbol,Underlying,SecurityName,ISINNumber\nNIFTYBEES,Nifty 50,BeES,INF204KB14I2\n"
NIFTYBEES = parse_isin("INF204KB14I2")


def test_an_incomplete_list_is_asked_for_again_at_most_hourly(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2 R-2 and verify-3 C-4 end to end: the ETF list fails once. The
    partial list is used, both lists are asked for again only after an hour,
    and a complete list is then kept for the day."""
    equity = (FIX / "live" / "EQUITY_L-sample.csv").read_bytes()
    etf_answers: list[bytes | None] = [None, ETF_LIST, ETF_LIST]
    asked: list[str] = []

    def get(url: str) -> bytes | None:
        asked.append(url)
        return equity if url == live.EQUITY_LIST_URL else etf_answers.pop(0)

    fetch = live.fetch_symbols
    monkeypatch.setattr(live, "fetch_symbols", lambda: fetch(get))
    symbols = cli_mod._symbols  # pyright: ignore[reportPrivateUsage]
    first, complete = symbols(MON)
    assert NIFTYBEES not in first and complete is False and len(asked) == 2
    again, complete = symbols(MON + timedelta(minutes=59))
    assert again == first and complete is False and len(asked) == 2  # not every pass
    second, complete = symbols(MON + timedelta(minutes=61))
    assert second[NIFTYBEES] == "NIFTYBEES" and complete is True and len(asked) == 4
    third, complete = symbols(MON + timedelta(hours=2))
    assert third == second and complete is True and len(asked) == 4  # kept for the day
    _, complete = symbols(MON + timedelta(days=1))
    assert complete is True and len(asked) == 6  # a new day asks again


def test_both_nse_lists_failing_says_the_list_could_not_be_downloaded() -> None:
    """V3-6 survivor: with nothing back, the reason is the download, not an
    incomplete list."""
    with pytest.raises(live.LiveRejected, match="could not download NSE's list of symbols"):
        live.fetch_symbols(lambda url: None)
    with pytest.raises(live.LiveRejected, match="could not download"):
        live.fetch_symbols(lambda url: b"<html>Access denied</html>")


def test_one_unreadable_nse_list_does_not_drop_the_other() -> None:
    """V2-6 survivor: NSE changes one file's header, or answers with a page."""
    equity = (FIX / "live" / "EQUITY_L-sample.csv").read_bytes()

    def get(url: str) -> bytes | None:
        return equity if url == live.EQUITY_LIST_URL else b"<html>Access denied</html>"

    found, complete = live.fetch_symbols(get)
    assert found[INFY] == "INFY" and complete is False

    def other(url: str) -> bytes | None:
        return b"NAME,CODE\nx,y\n" if url == live.EQUITY_LIST_URL else ETF_LIST

    found, complete = live.fetch_symbols(other)
    assert found == {NIFTYBEES: "NIFTYBEES"} and complete is False


def test_a_404_on_a_quote_is_no_quote_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """V2-5 survivor."""
    import urllib.error
    import urllib.request
    from email.message import Message

    def gone(req: urllib.request.Request, timeout: float) -> object:
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", Message(), None)

    monkeypatch.setattr(urllib.request, "urlopen", gone)
    with pytest.raises(LiveRejected, match="INFY: no quote found"):
        live.fetch_quote(INFY, "INFY")


def test_todays_official_close_is_shown_as_what_verdicts_use(data: Path, net: Net) -> None:
    """Round 2 C-2: once today's bhavcopy is in, it beats the day's last
    live trade, so `prices live` says CLOSE and asks for no quote."""
    today = data / "bhavcopy-today.csv"
    sample = (FIX / "eod" / "bhavcopy-sample.csv").read_text(encoding="utf-8")
    today.write_text(sample.replace("2026-11-14", "2026-11-16"), encoding="utf-8")
    assert runner.invoke(cli, ["prices", "import", str(today)]).exit_code in (0, 1)
    view = json.loads(runner.invoke(cli, ["prices", "live", "--json"]).output)
    assert view["live"] == [] and net.asked == [] and net.lists == 0
    assert view["end_of_day"] == [
        {
            "isin": INFY,
            "reason": "today's official close of ₹1,500.55 is in; verdicts use it",
        }
    ]
    human = runner.invoke(cli, ["prices", "live"]).output
    assert "LIVE" not in human and f"CLOSE {INFY}" in human


def test_only_todays_exchange_close_stands_in_for_a_live_price(
    data: Path, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NAV dated today (an ETF has one) ranks below a live trade, so the
    live price is still asked for and used."""
    nav = Quote(isin=INFY, on=MON.date(), price=Price.rupees("1500"), source="amfi")
    monkeypatch.setattr(cli_mod, "_price_book", lambda held: PriceBook(quotes={INFY: nav}))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    view = json.loads(runner.invoke(cli, ["prices", "live", "--json"]).output)
    assert view["live"][0]["isin"] == INFY and view["end_of_day"] == []


def test_ctrl_c_during_a_pass_ends_watch(
    data: Path, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V2-1 survivor: the guard around a pass must not swallow Ctrl+C."""
    passes: list[int] = []

    def interrupted(send: bool) -> None:
        passes.append(1)
        raise KeyboardInterrupt

    def sleep(s: int) -> None:
        raise AssertionError("the watch went on after Ctrl+C")

    monkeypatch.setattr(cli_mod, "india_now", lambda: MON)
    monkeypatch.setattr(cli_mod, "_watch_pass", interrupted)
    monkeypatch.setattr(cli_mod, "_sleep", sleep)
    r = runner.invoke(cli, ["watch", "--send"])
    assert r.exit_code == 0 and passes == [1] and r.output.strip() == "stopped"


TOKEN = "123456:" + "A" * 35  # the shape of a bot token; synthetic


class Phone:
    """Stands in for Telegram: records what the watch sends."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.got: list[str] = []
        self.down = False
        monkeypatch.setenv("STOCKIN_TELEGRAM_TOKEN", TOKEN)
        monkeypatch.setenv("STOCKIN_TELEGRAM_CHAT", "555")
        monkeypatch.setattr(cli_mod.notify, "http_transport", lambda token: self.call)

    def call(self, method: str, params: dict[str, str]) -> object:
        if self.down:
            raise cli_mod.notify.TelegramError("could not reach Telegram (timed out)")
        assert method == "sendMessage" and params["chat_id"] == "555"
        self.got.append(params["text"])
        return None


def _run_watch(
    monkeypatch: pytest.MonkeyPatch, outcomes: Sequence[BaseException | None], *args: str
) -> str:
    """`watch` with one pass per outcome (None is a good pass), then Ctrl+C."""
    left = list(outcomes)

    def one_pass(send: bool) -> None:
        outcome = left.pop(0)
        if outcome is not None:
            raise outcome

    def sleep(s: int) -> None:
        if not left:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "india_now", lambda: MON)
    monkeypatch.setattr(cli_mod, "_watch_pass", one_pass)
    monkeypatch.setattr(cli_mod, "_sleep", sleep)
    r = runner.invoke(cli, ["watch", *args])
    assert r.exit_code == 0 and left == [], r.output
    return r.output


def test_a_watch_that_keeps_failing_tells_telegram_once_per_streak(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2 C-1: nobody reads stderr on a 24/7 setup."""
    phone = Phone(monkeypatch)
    boom = RuntimeError("database disk image\nis malformed")
    out = _run_watch(monkeypatch, [boom] * 2, "--send")
    assert phone.got == [] and "in a row" not in out  # two failed passes can be a blip
    out = _run_watch(monkeypatch, [boom] * 6, "--send")
    notice = (
        "Stockin watch cannot run: RuntimeError: database disk image is malformed. "
        "It has failed 3 checks in a row and keeps trying."
    )
    assert phone.got == [notice]  # one line, once, however long the streak
    assert out.count("error: watch has failed 3 checks in a row; told Telegram") == 1
    assert out.count("this pass failed") == 6
    phone.got.clear()
    out = _run_watch(monkeypatch, [boom] * 3 + [None] + [boom] * 2 + [None] + [boom] * 3, "--send")
    assert len(phone.got) == 2  # a good pass ends the streak; the short one says nothing


def test_the_failure_notice_carries_the_reason_already_said(
    data: Path, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that ends with a said error (typer.Exit) counts too, and the
    notice carries that error, here a prices.db that is not a database."""
    phone = Phone(monkeypatch)
    (data / "prices.db").write_bytes(b"not a database, " * 64)
    slept: list[int] = []

    def sleep(s: int) -> None:
        slept.append(s)
        if len(slept) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "india_now", lambda: MON)
    monkeypatch.setattr(cli_mod, "_sleep", sleep)
    r = runner.invoke(cli, ["watch", "--send"])
    assert r.exit_code == 0, r.output
    (notice,) = phone.got
    assert notice.startswith("Stockin watch cannot run: ") and "prices.db" in notice
    assert "\n" not in notice


def test_the_failure_notice_is_tried_again_until_telegram_takes_it(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone = Phone(monkeypatch)
    phone.down = True
    boom = RuntimeError("something new")
    left: list[BaseException | None] = [boom] * 5

    def one_pass(send: bool) -> None:
        if len(left) == 1:
            phone.down = False  # Telegram is back for the fifth failed pass
        raise left.pop(0)  # type: ignore[misc]

    def sleep(s: int) -> None:
        if not left:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "india_now", lambda: MON)
    monkeypatch.setattr(cli_mod, "_watch_pass", one_pass)
    monkeypatch.setattr(cli_mod, "_sleep", sleep)
    r = runner.invoke(cli, ["watch", "--send"])
    assert r.output.count("did not reach Telegram") == 2  # the third and fourth pass
    assert len(phone.got) == 1 and "failed 5 checks in a row" in phone.got[0]


def test_the_failure_notice_never_holds_the_token_and_needs_send(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone = Phone(monkeypatch)
    boom = RuntimeError(f"could not open https://example.invalid/bot{TOKEN}/x")
    out = _run_watch(monkeypatch, [boom] * 3, "--send")
    (notice,) = phone.got
    assert TOKEN not in notice and "***" in notice and TOKEN not in out
    phone.got.clear()
    out = _run_watch(monkeypatch, [RuntimeError("x")] * 4)  # no --send: stderr only
    assert phone.got == []
    assert out.count("error: watch has failed 3 checks in a row") == 1


def test_a_failing_watch_with_no_telegram_says_so(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _run_watch(monkeypatch, [RuntimeError("x")] * 4, "--send")
    assert out.count("Telegram is not connected, so no notice was sent") == 1


def test_watch_once_send_sends(data: Path, net: Net, monkeypatch: pytest.MonkeyPatch) -> None:
    """V9 survivor."""
    sent: list[bool] = []
    monkeypatch.setattr(cli_mod, "_send_pending", lambda quiet=False: sent.append(True))
    assert runner.invoke(cli, ["watch", "--once", "--send"]).exit_code == 0
    assert runner.invoke(cli, ["watch", "--once"]).exit_code == 0
    assert sent == [True]


def test_watch_survives_an_unexpected_error_in_a_pass(
    data: Path, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-1: only Ctrl+C ends a watch."""
    passes: list[int] = []

    def boom(send: bool) -> None:
        passes.append(1)
        raise RuntimeError("something new")

    slept: list[int] = []

    def sleep(s: int) -> None:
        slept.append(s)
        if len(slept) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "india_now", lambda: MON)
    monkeypatch.setattr(cli_mod, "_watch_pass", boom)
    monkeypatch.setattr(cli_mod, "_sleep", sleep)
    r = runner.invoke(cli, ["watch"])
    assert r.exit_code == 0 and len(passes) == 2
    assert "this pass failed (RuntimeError: something new); trying again" in r.output


def test_a_quote_is_asked_for_once_with_a_short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """C-3: a slow or dead source costs one holding QUOTE_TIMEOUT seconds a
    pass, not three attempts with pauses."""
    import urllib.error
    import urllib.request

    asked: list[float] = []

    def dead(req: object, timeout: float) -> object:
        asked.append(timeout)
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", dead)
    with pytest.raises(LiveRejected):
        live.fetch_quote(INFY, "INFY")
    assert asked == [live.QUOTE_TIMEOUT] and live.QUOTE_TIMEOUT <= 10


def test_the_notice_carries_the_error_not_its_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V3-7 survivor: with no ledger, the notice says what is wrong (no
    ledger), not only what to do about it."""
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    phone = Phone(monkeypatch)
    slept: list[int] = []

    def sleep(s: int) -> None:
        slept.append(s)
        if len(slept) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "india_now", lambda: MON)
    monkeypatch.setattr(cli_mod, "_sleep", sleep)
    r = runner.invoke(cli, ["watch", "--send"])
    assert r.exit_code == 0, r.output
    (notice,) = phone.got
    assert "no ledger at" in notice and "import a statement first" not in notice


def test_a_token_in_the_data_folder_path_is_never_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#33 verify-3 C-3 (crafted): the `no ledger at` line strikes it."""
    folder = tmp_path / TOKEN / "data"
    folder.mkdir(parents=True)
    monkeypatch.setenv("STOCKIN_DATA", str(folder))
    Phone(monkeypatch)
    r = runner.invoke(cli, ["watch", "--once"])
    assert r.exit_code == 2 and "no ledger at" in r.output
    assert TOKEN not in r.output and "***" in r.output


def test_watch_once_on_a_damaged_ledger_says_one_line(
    data: Path, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#33 verify-3 C-2: no traceback, one error line, exit 1."""
    (data / "ledger.db").write_bytes(b"not a database, " * 64)
    monkeypatch.setattr(cli_mod, "india_now", lambda: MON)
    r = runner.invoke(cli, ["watch", "--once"])
    assert r.exit_code == 1, r.output
    assert "Traceback" not in r.output and "error: this check failed (" in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_no_list_at_all_is_asked_for_again_only_hourly(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#39 verifier C-2: both of NSE's lists failing is not downloaded again
    on every pass either."""
    asked: list[str] = []

    def get(url: str) -> bytes | None:
        asked.append(url)
        return None

    fetch = live.fetch_symbols
    monkeypatch.setattr(live, "fetch_symbols", lambda: fetch(get))
    symbols = cli_mod._symbols  # pyright: ignore[reportPrivateUsage]
    for minutes in (0, 30, 59):
        with pytest.raises(live.LiveRejected, match="could not download"):
            symbols(MON + timedelta(minutes=minutes))
    assert len(asked) == 2
    with pytest.raises(live.LiveRejected):
        symbols(MON + timedelta(minutes=61))
    assert len(asked) == 4


def test_a_price_that_is_not_one_names_its_symbol() -> None:
    """#39 verifier C-4."""
    for price in ("1e400", "0.004"):
        data = chart("INFY.NS", price, MON)
        with pytest.raises(live.LiveRejected, match=r"^INFY: "):
            live.parse_chart(data, INFY, "INFY")


def test_watch_once_strikes_the_token_from_an_unexpected_error(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#39 verifier V12 survivor."""
    Phone(monkeypatch)

    def boom(send: bool) -> None:
        raise RuntimeError(f"could not open https://example.invalid/bot{TOKEN}/x")

    monkeypatch.setattr(cli_mod, "_watch_pass", boom)
    r = runner.invoke(cli, ["watch", "--once"])
    assert r.exit_code == 1 and "error: this check failed (RuntimeError" in r.output
    assert TOKEN not in r.output and "***" in r.output


# ------------------------------------------------------------ #39 verifier, round 2

BEES = parse_isin("INF204KB14I2")


def _demat(tmp: Path, start: str, end: str, shares: dict[str, tuple[str, str]]) -> str:
    """A synthetic NSDL statement, the shape of the verifier's `demat-rebuy-*`."""
    body = {
        "accounts": [
            {
                "name": "Test Broker",
                "type": "NSDL Demat Account",
                "dp_id": "IN300000",
                "client_id": "10000001",
                "folios": 0,
                "balance": "0",
                "owners": [{"name": "Test Investor", "PAN": "AAAAA0000A"}],
                "equities": [
                    {"name": name, "isin": isin, "num_shares": n, "price": "1", "value": n}
                    for isin, (name, n) in shares.items()
                ],
                "mutual_funds": [],
                "bonds": [],
            }
        ],
        "statement_period": {"from": start, "to": end},
        "investor_info": {
            "name": "Test Investor",
            "email": "test@example.invalid",
            "address": "-",
            "mobile": "-",
        },
        "file_type": "NSDL",
        "nps": None,
        "parse_warnings": [],
    }
    path = tmp / f"demat-{end}.json"
    path.write_text(json.dumps(body))
    return str(path)


def _bhav(tmp: Path, day: str, closes: dict[str, tuple[str, str]]) -> str:
    header = (FIX / "eod" / "bhavcopy-sample.csv").read_text().splitlines()[0]
    rows = [
        f"{day},{day},CM,NSE,STK,1,{isin},{symbol},EQ,,,,,{symbol} LTD,1,1,1,"
        f"{close},{close},1,,{close},,,1,1,1,F1,1,,,,,"
        for isin, (symbol, close) in closes.items()
    ]
    path = tmp / f"bhavcopy-{day}.csv"
    path.write_text("\n".join([header, *rows]) + "\n")
    return str(path)


class Rebuy:
    """The verifier's flowK, value for value: Infosys held in February at a
    close of ₹1,900.00, sold in March, bought back in October; the statement
    is imported on Monday 2026-11-16 after the close, and that evening's
    price file says the true close, ₹1,500.55."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp, self.now = tmp, datetime(2026, 3, 2, 19, 0, tzinfo=IST)
        self.live = {"INFY": "1502.00", "NIFTYBEES": "288.00"}
        monkeypatch.setenv("STOCKIN_DATA", str(tmp / "data"))
        monkeypatch.setattr(prices_mod, "_now", lambda: self.now)
        monkeypatch.setattr(
            live, "fetch_symbols", lambda: ({INFY: "INFY", BEES: "NIFTYBEES"}, True)
        )
        monkeypatch.setattr(live, "fetch_quote", self.quote)
        self.phone = Phone(monkeypatch)
        infy, bees = ("INFOSYS LIMITED", "12"), ("NIPPON INDIA ETF NIFTY 50 BEES", "40")
        both = {str(INFY): infy, str(BEES): bees}
        self.run("import", "cas", _demat(tmp, "01-Feb-2026", "28-Feb-2026", both))
        self.run("prices", "import", self.file("2026-02-27", "1900.00"))
        self.at(2026, 4, 3, 19)
        self.run("import", "cas", _demat(tmp, "01-Mar-2026", "31-Mar-2026", {str(BEES): bees}))
        self.at(2026, 11, 16, 19)
        self.run("import", "cas", _demat(tmp, "01-Oct-2026", "31-Oct-2026", both))
        self.run("thesis", "set", INFY, "--why", "synthetic: cash machine", "--target", "58")

    def at(self, *when: int) -> None:
        self.now = datetime(*when, tzinfo=IST)  # type: ignore[misc]

    def file(self, day: str, infy: str, bees: str = "287.61") -> str:
        closes = {str(INFY): ("INFY", infy), str(BEES): ("NIFTYBEES", bees)}
        return _bhav(self.tmp, day, closes)

    def quote(self, isin: Isin, symbol: str) -> LiveQuote:
        at = self.now - timedelta(minutes=1)
        return live.parse_chart(chart(f"{symbol}.NS", self.live[symbol], at), isin, symbol)

    def run(self, *args: str, code: int = 0) -> str:
        r = runner.invoke(cli, list(args))
        assert r.exit_code == code, r.output
        return r.output


def test_a_share_bought_back_after_months_sends_no_trim_on_its_old_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#39 verifier round 2, R-1. The evening file's true close is held
    against February's, so verdicts were worked out from ₹1,900.00 (66.5%,
    TRIM; the true weight is 61.0%, HOLD) and the next morning's
    `watch --once --send` sent that TRIM to Telegram."""
    flow = Rebuy(tmp_path, monkeypatch)
    flow.at(2026, 11, 16, 20)
    evening = flow.file("2026-11-16", "1500.55")
    out = flow.run("prices", "import", evening, code=1)
    assert "NOT stored: INE009A01021 was 1,900.00 on 2026-02-27" in out
    assert "this file says 1,500.55 on 2026-11-16" in out
    # C-2: the hold text covers this case, not only a split or a damaged file
    for part in ("no add or trim is worked out", "a long break", "bought back", "--accept-jumps"):
        assert part in out, part
    said = flow.run("verdicts")
    assert "TRIM" not in said and "66.5%" not in said
    (infy,) = [line for line in said.splitlines() if "INFOSYS" in line]
    assert infy.startswith("REVIEW ")
    for part in ("₹1,500.55", "2026-11-16", "₹1,900.00", "2026-02-27", "--accept-jumps"):
        assert part in infy, part
    flow.run("alerts", "check", "--send")
    flow.at(2026, 11, 17, 9, 20)
    out = flow.run("watch", "--once", "--send")
    assert "1 live, 1 at close, 0 new alerts" in out, out
    assert flow.phone.got == []
    view = json.loads(flow.run("prices", "live", "--json"))
    (kept,) = view["end_of_day"]
    assert kept["isin"] == INFY and "too old" not in kept["reason"]
    for part in ("INFY", "₹1,500.55", "2026-11-16", "--accept-jumps"):
        assert part in kept["reason"], part
    # the owner looks, and accepts the price: now the true weight speaks
    flow.run("prices", "import", evening, "--accept-jumps")
    assert "HOLD   INFOSYS is 61.0% of your money" in flow.run("verdicts")
    out = flow.run("watch", "--once", "--send")
    assert "2 live, 0 at close, 0 new alerts" in out, out
    assert flow.phone.got == []


def test_a_held_price_sends_no_add_for_the_other_holding_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same flow from the other side: with Infosys at February's
    ₹1,900.00 the ETF looks like 33.5% of the money, below a 39% target by
    more than 5 points (ADD); at the true close it is 39.0% (HOLD). Every
    weight depends on the price that is held, so none drives a call."""
    flow = Rebuy(tmp_path, monkeypatch)
    flow.run("thesis", "set", BEES, "--why", "synthetic: the index", "--target", "39")
    flow.at(2026, 11, 16, 20)
    evening = flow.file("2026-11-16", "1500.55")
    flow.run("prices", "import", evening, code=1)
    said = flow.run("verdicts")
    assert "ADD" not in said and "TRIM" not in said
    (bees,) = [line for line in said.splitlines() if "BEES" in line]
    assert bees.startswith("REVIEW ") and "INFOSYS" in bees and "₹1,500.55" in bees
    flow.run("alerts", "check", "--send")
    flow.at(2026, 11, 17, 9, 20)
    flow.run("watch", "--once", "--send")
    assert flow.phone.got == []
    flow.run("prices", "import", evening, "--accept-jumps")
    said = flow.run("verdicts")
    assert "HOLD   INFOSYS is 61.0%" in said and "is 39.0% of your money" in said
    assert "ADD" not in said and "TRIM" not in said


def test_a_split_held_for_a_week_makes_no_call_and_says_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 1's R-1 from the verdict side: the split stays out every
    evening, and while it does, no add or trim is worked out; the morning
    after the stored close turns 8 days old nothing is sent either."""
    flow = Rebuy(tmp_path, monkeypatch)
    flow.at(2026, 11, 13, 20)
    flow.run("prices", "import", flow.file("2026-11-13", "1500.55"), "--accept-jumps")
    flow.run("thesis", "set", INFY, "--why", "synthetic: cash machine", "--target", "70")
    assert "ADD    INFOSYS is 61.0%" in flow.run("verdicts")
    flow.live["INFY"] = "752.00"
    for day in (16, 17, 18, 19, 20, 23):
        flow.at(2026, 11, day, 20)
        out = flow.run("prices", "import", flow.file(f"2026-11-{day}", "750.30"), code=1)
        assert "NOT stored: INE009A01021 was 1,500.55 on 2026-11-13" in out
        said = flow.run("verdicts")
        assert "REVIEW INFOSYS" in said and "ADD" not in said and "TRIM" not in said
        assert f"₹750.30 on 2026-11-{day}" in said
        flow.run("alerts", "check", "--send")
        # the next morning, inside the week: the live price is beyond the band
        flow.at(2026, 11, day + 1, 9, 20)
        (kept,) = json.loads(flow.run("prices", "live", "--json"))["end_of_day"]
        assert f"₹750.30 on 2026-11-{day}" in kept["reason"], kept
        assert "--accept-jumps" in kept["reason"] and "a bad quote" not in kept["reason"]
    flow.at(2026, 11, 24, 9, 20)
    out = flow.run("watch", "--once", "--send")
    assert "1 live, 1 at close, 0 new alerts" in out
    assert flow.phone.got == []
    (kept,) = json.loads(flow.run("prices", "live", "--json"))["end_of_day"]
    assert "₹750.30 on 2026-11-23" in kept["reason"] and "--accept-jumps" in kept["reason"]
    assert "₹1,500.55 on 2026-11-13" in kept["reason"] and "too old" not in kept["reason"]


def test_a_price_older_than_a_week_makes_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no price file at all for more than BAND_DAYS, nothing is held,
    but the last close is still not a price to act on: REVIEW, with the
    price, its day and what to run. Seven days old is still used."""
    flow = Rebuy(tmp_path, monkeypatch)
    flow.at(2026, 11, 13, 20)
    flow.run("prices", "import", flow.file("2026-11-13", "1500.55"), "--accept-jumps")
    flow.run("thesis", "set", INFY, "--why", "synthetic: cash machine", "--target", "70")
    flow.at(2026, 11, 20, 20)
    assert "ADD    INFOSYS is 61.0%" in flow.run("verdicts")
    flow.at(2026, 11, 21, 20)
    said = flow.run("verdicts")
    assert "ADD" not in said
    (infy,) = [line for line in said.splitlines() if "INFOSYS" in line]
    assert infy.startswith("REVIEW ") and "--accept-jumps" not in infy
    for part in ("₹1,500.55", "2026-11-13", "more than 7 days", "stockin prices fetch"):
        assert part in infy, part
    flow.run("alerts", "check", "--send")
    assert flow.phone.got == []


def test_watch_makes_no_call_from_a_close_older_than_a_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule on the path that sends with no one looking. On the 7th
    day the close is still used (with the live price laid over it) and a
    true ADD goes out; from the 8th day the live price is not used and the
    old close gives no call, whatever the target says."""
    flow = Rebuy(tmp_path, monkeypatch)
    flow.at(2026, 11, 13, 20)
    flow.run("prices", "import", flow.file("2026-11-13", "1500.55"), "--accept-jumps")
    flow.run("thesis", "set", INFY, "--why", "synthetic: cash machine", "--target", "70")
    flow.at(2026, 11, 20, 9, 20)
    out = flow.run("watch", "--once", "--send")
    assert "2 live, 0 at close, 1 new alert" in out and "ADD" in out
    assert [text.splitlines()[0] for text in flow.phone.got] == ["ADD INFOSYS"]
    flow.at(2026, 11, 23, 9, 20)
    flow.run("thesis", "set", INFY, "--why", "synthetic: cash machine", "--target", "50")
    out = flow.run("watch", "--once", "--send")
    assert "0 live, 2 at close, 0 new alerts" in out, out
    assert len(flow.phone.got) == 1  # no TRIM at 61% from a close 10 days old


def test_a_price_beyond_any_real_one_names_its_symbol() -> None:
    """#39 verify-2 survivor M8."""
    data = chart("INFY.NS", "123456789.00", MON)
    with pytest.raises(live.LiveRejected, match=r"^INFY: 123456789\.00 is beyond any real price$"):
        live.parse_chart(data, INFY, "INFY")
