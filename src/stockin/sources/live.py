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
import io
import json
import urllib.parse
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
    except (ValueError, KeyError, IndexError, TypeError):
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


def fetch_symbols(get: Get = eod._get) -> dict[Isin, str]:  # pyright: ignore[reportPrivateUsage]
    out: dict[Isin, str] = {}
    for url in (EQUITY_LIST_URL, ETF_LIST_URL):
        try:
            data = get(url)
        except OSError:
            continue
        if data is not None:
            try:
                out |= parse_symbols(data)
            except LiveRejected:
                continue
    if not out:
        raise LiveRejected("could not download NSE's list of symbols")
    return out


def _get_quick(url: str) -> bytes | None:
    return eod._get(url, timeout=15)  # pyright: ignore[reportPrivateUsage]


def fetch_quote(isin: Isin, symbol: str, get: Get = _get_quick) -> LiveQuote:
    try:
        data = get(CHART_URL.format(symbol=urllib.parse.quote(f"{symbol}.NS")))
    except OSError as exc:
        raise LiveRejected(
            f"{symbol}: could not reach the quote source ({type(exc).__name__})"
        ) from None
    if data is None:
        raise LiveRejected(f"{symbol}: no quote found")
    return parse_chart(data, isin, symbol)


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
