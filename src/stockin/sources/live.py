"""Live share prices from public quotes: no login, no token, no broker.

Two public sources, both read-only:
- NSE's list of listed equities (EQUITY_L.csv), to turn the ISINs you hold
  into NSE symbols;
- Yahoo Finance's chart endpoint, for each symbol's last traded price. It is
  unofficial and a few minutes behind the exchange. If it fails or answers
  with anything odd, that holding keeps its end-of-day close and the run
  says so: a live price is never guessed.

Prices arrive as JSON numbers. They are read as decimals (never floats) and
rounded half-even to the paise, the exchange's own tick.

Parsers are pure functions of the bytes; `fetch_*` are the only network calls.
"""

from __future__ import annotations

import csv
import http.client
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from ..facts import Isin, parse_isin
from ..money import Price, Value
from ..prices import MAX_PRICE_MICROS, Quote
from . import eod

EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
ETF_LIST_URL = "https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"

IST = timezone(timedelta(hours=5, minutes=30))
OPEN, CLOSE = time(9, 15), time(15, 30)
"""NSE's normal market, Monday to Friday. Holidays are not listed here: on
a holiday the newest trade is from an earlier day, so it is not live."""

MAX_AGE = timedelta(minutes=20)
"""A quote older than this while the market is open is not called live."""


class LiveRejected(ValueError):
    """A public answer that is not a usable price."""


class LiveQuote(Value):
    isin: Isin
    symbol: str
    price: Price
    at: datetime  # the trade's time, India


def market_open(now: datetime) -> bool:
    t = now.astimezone(IST)
    return t.weekday() < 5 and OPEN <= t.time() <= CLOSE


def parse_symbols(data: bytes) -> dict[Isin, str]:
    """ISIN to NSE symbol from EQUITY_L.csv or the ETF list. Columns are
    found by header name (NSE pads them with spaces). Rows that are not a
    valid ISIN are skipped; an ISIN listed under two symbols is dropped,
    since nothing says which is right."""
    text = data.decode("utf-8-sig", errors="replace")
    rows = csv.reader(io.StringIO(text))
    empty: list[str] = []
    first = next(rows, empty)
    header = [h.strip().upper() for h in first]
    isin_col = next(
        (i for i, h in enumerate(header) if h in ("ISIN NUMBER", "ISINNUMBER", "ISIN")), None
    )
    if "SYMBOL" not in header or isin_col is None:
        raise LiveRejected("not an NSE securities list (no SYMBOL and ISIN NUMBER columns)")
    sym_col = header.index("SYMBOL")
    out: dict[Isin, str] = {}
    twice: set[Isin] = set()
    for row in rows:
        if len(row) <= max(sym_col, isin_col):
            continue
        try:
            isin = parse_isin(row[isin_col])
        except ValueError:
            continue
        symbol = row[sym_col].strip().upper()
        if not symbol or not all(c.isalnum() or c in "&-_" for c in symbol):
            continue
        if isin in out and out[isin] != symbol:
            twice.add(isin)
        out[isin] = symbol
    for isin in twice:
        del out[isin]
    if not out:
        raise LiveRejected("the NSE securities list has no rows")
    return out


def _paise(number: Decimal) -> Price:
    try:
        rupees = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        raise LiveRejected(f"{number} is not a price") from None
    if not rupees.is_finite() or rupees <= 0:
        raise LiveRejected(f"{number} is not a price")
    micros = int(rupees * 1_000_000)
    if micros > MAX_PRICE_MICROS:
        raise LiveRejected(f"{rupees} is beyond any real price")
    return Price(micro_rupees=micros)


def parse_chart(data: bytes, isin: Isin, symbol: str) -> LiveQuote:
    """The last traded price and its time from Yahoo's chart JSON for
    `symbol`.NS. Refuses an answer for another symbol, in another currency,
    or without a price and a time."""
    try:
        raw: Any = json.loads(data, parse_float=Decimal, parse_int=Decimal)
        meta: Any = raw["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        raise LiveRejected(f"{symbol}: the quote could not be read") from None
    if not isinstance(meta, dict):
        raise LiveRejected(f"{symbol}: the quote could not be read")
    if str(meta.get("symbol", "")).upper() != f"{symbol}.NS":  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        raise LiveRejected(f"{symbol}: the answer is for {meta.get('symbol')!r}")  # pyright: ignore[reportUnknownMemberType]
    if meta.get("currency") != "INR":  # pyright: ignore[reportUnknownMemberType]
        raise LiveRejected(f"{symbol}: the price is not in rupees")
    price, when = meta.get("regularMarketPrice"), meta.get("regularMarketTime")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if (
        not isinstance(price, Decimal)
        or not isinstance(when, Decimal)
        or when != when.to_integral()
    ):
        raise LiveRejected(f"{symbol}: the quote has no price or time")
    try:
        at = datetime.fromtimestamp(int(when), IST)
    except (OverflowError, OSError, ValueError):
        raise LiveRejected(f"{symbol}: the quote's time is not a time") from None
    return LiveQuote(isin=isin, symbol=symbol, price=_paise(price), at=at)


Get = Callable[[str], bytes | None]


_FETCH_ERRORS = (OSError, http.client.HTTPException, ValueError, RecursionError)
"""Every way one HTTP answer can go wrong: refused, reset, cut short
(IncompleteRead), a garbled status line (BadStatusLine), or bytes no parser
can hold (#33 verifier R-1). Each is a fallback to the close, never a crash."""


def fetch_symbols(get: Get = eod._get) -> tuple[dict[Isin, str], bool]:  # pyright: ignore[reportPrivateUsage]
    """The ISIN-to-symbol pairs from both NSE lists, and whether both came
    back. One list that fails, or cannot be read, does not drop the other:
    what came back is used for this pass. It is complete, and so kept for
    the day, only when both did; otherwise the next pass tries again (#33
    verifier R-3, and round 2's R-2 for the ETF list)."""
    out: dict[Isin, str] = {}
    lists = 0
    for url in (EQUITY_LIST_URL, ETF_LIST_URL):
        try:
            data = get(url)
            found = parse_symbols(data) if data is not None else {}
        except (*_FETCH_ERRORS, LiveRejected):
            continue
        out |= found
        lists += bool(found)
    if not out:
        raise LiveRejected("could not download NSE's list of symbols")
    return out, lists == 2


QUOTE_TIMEOUT = 10
"""Seconds for one quote, tried once: a slow source costs one holding ten
seconds per pass, not 45 (#33 verifier C-3)."""


def _get_once(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": eod._UA})  # pyright: ignore[reportPrivateUsage]
    try:
        with urllib.request.urlopen(req, timeout=QUOTE_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def fetch_quote(isin: Isin, symbol: str, get: Get = _get_once) -> LiveQuote:
    try:
        data = get(CHART_URL.format(symbol=urllib.parse.quote(f"{symbol}.NS")))
    except _FETCH_ERRORS as exc:
        raise LiveRejected(
            f"{symbol}: could not reach the quote source ({type(exc).__name__})"
        ) from None
    if data is None:
        raise LiveRejected(f"{symbol}: no quote found")
    return parse_chart(data, isin, symbol)


BAND_PERCENT = 20
"""NSE's widest daily price band. A live price further than this from the
last close is not one ordinary day's trading in most shares: it is a split
or bonus day (the units held still come from the last statement, so the
value would be wrong by the split's ratio), or a bad quote. The 2x rule that
price files use is too loose here, because a live price sends an alert with
no one looking: a 1:1 split that opens a paisa up is just inside 2x (#33
verifier, round 2). Shares in futures and options have no fixed band and
can, rarely, really move more than this in a day; such a day keeps the close
too, and says so, until the evening's official close is fetched.

What no band can see: a bonus of 1:4 or smaller moves the price by 20% or
less, like an ordinary day. That needs the exchange's list of corporate
actions, which Stockin does not read yet."""

MAX_CLOSE_AGE = timedelta(days=7)
"""The band is about one trading day, so a live price is checked only
against a close from the last week: long enough for any run of holidays and
a missed evening or two of `prices fetch`. Against an older close a large
move proves nothing (a share can really double in months), so the live price
is not checked and not used, and the reason says to fetch prices."""


def beyond_band(close: Price, new: Price) -> bool:
    """More than BAND_PERCENT away from `close`, up or down. Exactly on the
    band is a real day (a share locked at its limit). Whole micro-rupees."""
    return 100 * abs(new.micro_rupees - close.micro_rupees) > BAND_PERCENT * close.micro_rupees


def as_quote(q: LiveQuote, now: datetime) -> Quote | None:
    """A live quote as a price for today, if it is live: traded today, no
    more than MAX_AGE ago, while the market is open (or today's last trade
    after the close). None otherwise; the end-of-day close then stands."""
    today: date = now.astimezone(IST).date()
    at = q.at.astimezone(IST)
    if at.date() != today or at > now + timedelta(minutes=2):
        return None
    if market_open(now) and now - at > MAX_AGE:
        return None
    return Quote(isin=q.isin, on=today, price=q.price, source="live")
