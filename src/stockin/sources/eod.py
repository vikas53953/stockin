"""Public end-of-day price files: parse, and fetch.

- AMFI `NAVAll.txt`: every mutual fund scheme's latest NAV. Semicolon
  separated: Scheme Code; ISIN Div Payout/ISIN Growth; ISIN Div
  Reinvestment; Scheme Name; Plan; Option; Net Asset Value; Date. Older
  files have no Plan and Option columns, so the NAV and date are read from
  the end of the row. Section headings and blank lines have no semicolons.
- NSE cash-market bhavcopy (the UDiFF CSV, usually zipped): one row per
  instrument and series. ISIN, SctySrs and ClsPric are read by header name.

Parsers are pure functions of the file's bytes and `today` (no price is dated
after today). `fetch_*` are the only network calls, and they only download
public files: no login, no token.

Neither plain file has a checksum, so a damaged copy can still be a valid
file with a wrong price. The parsers reject what is impossible (a field
count, a price or a date no real file has); everything else is undone by
importing the right file (see prices.py).
"""

from __future__ import annotations

import csv
import http.client
import io
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from collections import Counter
from datetime import date, datetime, timedelta

from ..facts import Isin, parse_isin
from ..money import Price
from ..prices import MAX_PRICE_MICROS, OLDEST_FILE_DAY, OLDEST_NAV_YEARS, PriceFile, Quote

AMFI_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
NSE_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
_UA = "Mozilla/5.0 (stockin; personal read-only price download)"

_SERIES_RANK = {"EQ": 0, "BE": 1, "BZ": 2, "SM": 3, "ST": 4, "BL": 9}
"""One ISIN can trade in several series. The normal-market series wins, and
the block-deal window (BL) loses to any other: an InvIT's own series is IV."""
_OTHER_SERIES = 8


class EodRejected(ValueError):
    """The file is not a price file this module understands."""


def _isin(text: str) -> Isin | None:
    try:
        return parse_isin(text)
    except ValueError:
        return None


def _price(text: str, where: str) -> Price | None:
    """None for a cell that is not a positive number ("N.A.", "-"). A
    number above MAX_PRICE_RUPEES a unit is not a real Indian price; it is
    a damaged file, so the whole file is rejected rather than stored."""
    try:
        price = Price.rupees(text)
    except ValueError:
        return None
    if price.micro_rupees > MAX_PRICE_MICROS:
        raise EodRejected(f"{where}: price {text.strip()} is beyond any real price (damaged file?)")
    return price


AMFI_OLDEST_YEARS = OLDEST_NAV_YEARS
"""NAVAll still lists wound-up schemes with their last NAV: in November 2026
the oldest row is from October 2008, 18 years back. A row more than 30 years
older than the file's day is not a real NAV row; it is a damaged year."""


def parse_amfi(text: str, today: date) -> PriceFile:
    """Every (ISIN, NAV, date) in a NAVAll file, and the file's day: the date
    most rows carry. Rows whose NAV is not a positive number (e.g. "N.A.") or
    whose ISIN is "-" are skipped. Both ISIN columns get the NAV:
    payout/growth and reinvestment units share it.

    Rejected: a row with more or fewer fields than the header, a NAV row
    whose date is not a date, is after `today`, or is more than
    AMFI_OLDEST_YEARS before the file's day. An ISIN listed twice keeps its
    later date; with two different NAVs for one date it gets no price from
    this file, because nothing says which is right."""
    lines = text.splitlines()
    width = lines[0].count(";") + 1 if lines else 0
    if width < 6 or not lines[0].startswith("Scheme Code;"):
        raise EodRejected("not an AMFI NAVAll file (first line is not its header)")
    out: dict[Isin, Quote] = {}
    unclear: set[Isin] = set()
    days: Counter[date] = Counter()
    for n, line in enumerate(lines[1:], start=2):
        if ";" not in line:
            continue  # a section heading or a blank line
        cells = [c.strip() for c in line.split(";")]
        if len(cells) != width:
            # two rows run together, or a lost semicolon: the NAV and date
            # would be read from the wrong place
            raise EodRejected(
                f"NAVAll line {n} has {len(cells)} fields but the header has {width} "
                "(damaged file?)"
            )
        price = _price(cells[-2], f"NAVAll line {n}")
        if price is None:
            continue
        try:
            on = datetime.strptime(cells[-1], "%d-%b-%Y").date()
        except ValueError:
            raise EodRejected(
                f"NAVAll line {n}: {cells[-1]!r} is not a date (damaged file?)"
            ) from None
        if on > today:
            raise EodRejected(f"NAVAll line {n} is dated {on}, after today (damaged file?)")
        days[on] += 1
        for cell in (cells[1], cells[2]):
            isin = _isin(cell)
            if isin is None:
                continue
            quote = Quote(isin=isin, on=on, price=price, source="amfi")
            prev = out.get(isin)
            if prev is None or quote.on > prev.on:
                out[isin] = quote
                unclear.discard(isin)
            elif quote.on == prev.on and quote.price != prev.price:
                unclear.add(isin)
    if not out:
        raise EodRejected("the NAVAll file has no NAV rows")
    day = max(days, key=lambda d: (days[d], d))
    if day < OLDEST_FILE_DAY:
        raise EodRejected(f"the NAVAll file is dated {day}, before any real file (damaged file?)")
    oldest = min(days)
    if oldest.year < day.year - AMFI_OLDEST_YEARS:
        raise EodRejected(
            f"the NAVAll file has a NAV dated {oldest}, more than {AMFI_OLDEST_YEARS} years "
            f"before its day {day} (damaged file?)"
        )
    quotes = tuple(out[k] for k in sorted(out) if k not in unclear)
    return PriceFile(source="amfi", day=day, quotes=quotes)


def _csv_text(data: bytes) -> str:
    """The CSV inside a bhavcopy. A truncated or corrupt zip, or text that is
    not UTF-8, is a rejected file, never a traceback."""
    try:
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".csv")]
                if len(names) != 1:
                    raise EodRejected("bhavcopy zip should hold exactly one CSV")
                data = z.read(names[0])
        text = data.decode("utf-8-sig")
        if text and not text.endswith("\n"):
            # NSE's files end with a newline; one that stops mid-row was cut off
            raise EodRejected("the bhavcopy CSV ends mid-row (an incomplete download?)")
        return text
    except UnicodeDecodeError:
        raise EodRejected(
            "not UTF-8 text, so neither an NSE bhavcopy nor an AMFI NAVAll file"
        ) from None
    except EodRejected:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError, RuntimeError) as exc:
        # zipfile reports a damaged archive as any of these (a flipped
        # "encrypted" bit is RuntimeError, a bad directory offset ValueError)
        raise EodRejected(f"the bhavcopy zip is damaged or incomplete ({exc})") from None


def parse_bhavcopy(data: bytes, today: date) -> PriceFile:
    """Close price per ISIN from an NSE UDiFF cash-market bhavcopy (zip or
    CSV), and its trading day. Prefers the EQ series when an ISIN trades in
    several. A CSV the csv module cannot read (a stray quote, a bare CR) is
    rejected, and so is a trading day after `today`."""
    text = _csv_text(data)
    try:
        return _bhavcopy_rows(text, today)
    except csv.Error as exc:
        raise EodRejected(f"the bhavcopy CSV is damaged ({exc})") from None


_NEED = ("TradDt", "ISIN", "SctySrs", "ClsPric")


def _bhavcopy_rows(text: str, today: date) -> PriceFile:
    """Columns are found by header name, and every row must have exactly as
    many fields as the header: a lost or extra comma anywhere (header or
    row) would otherwise shift a column, and the day's low or open would be
    stored as the close without any error. A bhavcopy is one trading day, so
    every row must carry the same TradDt. An ISIN with two different closes
    in equally ranked rows gets no price: nothing says which is right."""
    rows = csv.reader(io.StringIO(text), strict=True)
    first = next(rows, None)
    header = [] if first is None else [h.strip() for h in first]
    if not set(_NEED) <= set(header):
        raise EodRejected("not an NSE UDiFF bhavcopy (missing TradDt/ISIN/SctySrs/ClsPric)")
    if len(set(header)) != len(header):
        raise EodRejected("the bhavcopy header names a column twice (damaged file?)")
    at = {name: header.index(name) for name in _NEED}
    best: dict[Isin, tuple[int, Price]] = {}
    unclear: set[Isin] = set()
    day_text: str | None = None
    for n, row in enumerate(rows, start=2):
        if any("\n" in cell or "\r" in cell for cell in row):
            # NSE never quotes a line break; a stray quote has swallowed rows
            raise EodRejected(f"bhavcopy row {n} runs over several lines (damaged file?)")
        if len(row) != len(header):
            raise EodRejected(
                f"bhavcopy row {n} has {len(row)} fields but the header has {len(header)} "
                "(damaged file?)"
            )
        trad_dt = row[at["TradDt"]].strip()
        if day_text is None:
            day_text = trad_dt
        elif trad_dt != day_text:
            raise EodRejected(
                f"bhavcopy row {n} is dated {trad_dt!r} but the file is for {day_text} "
                "(damaged file?)"
            )
        isin = _isin(row[at["ISIN"]])
        price = _price(row[at["ClsPric"]], f"bhavcopy row {n}")
        if isin is None or price is None:
            continue
        rank = _SERIES_RANK.get(row[at["SctySrs"]].strip(), _OTHER_SERIES)
        prev = best.get(isin)
        if prev is None or rank < prev[0]:
            best[isin] = (rank, price)
            unclear.discard(isin)
        elif rank == prev[0] and price != prev[1]:
            unclear.add(isin)
    if not best:
        raise EodRejected("the bhavcopy has no price rows")
    try:
        day = date.fromisoformat(day_text or "")
    except ValueError:
        raise EodRejected(f"the bhavcopy's TradDt {day_text!r} is not a date") from None
    if day > today:
        raise EodRejected(f"the bhavcopy is dated {day}, after today (damaged file?)")
    if day < OLDEST_FILE_DAY:
        raise EodRejected(f"the bhavcopy is dated {day}, before any real file (damaged file?)")
    quotes = tuple(
        Quote(isin=isin, on=day, price=best[isin][1], source="bhavcopy")
        for isin in sorted(best)
        if isin not in unclear
    )
    return PriceFile(source="bhavcopy", day=day, quotes=quotes)


_sleep = time.sleep
ATTEMPTS = 3


def _get(url: str, timeout: float = 60) -> bytes | None:
    """GET a public file. None on 404 (no file for that day). A dropped
    connection (reset, or closed mid-download), or a 5xx, is retried twice,
    2 s then 4 s later: NSE resets connections from clients that ask again
    quickly."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for attempt in range(ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code < 500 or attempt == ATTEMPTS - 1:
                raise
        except (OSError, http.client.HTTPException):
            if attempt == ATTEMPTS - 1:
                raise
        _sleep(2 * 2**attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def fetch_amfi(today: date) -> PriceFile:
    data = _get(AMFI_URL)
    if data is None:
        raise EodRejected("AMFI NAVAll.txt was not found")
    return parse_amfi(data.decode("utf-8-sig", errors="replace"), today)


def fetch_bhavcopy(today: date, days_back: int = 7) -> PriceFile:
    """The newest bhavcopy on or before `today`, looking back over weekends
    (not asked for) and holidays (404). Rejected if no weekday in `days_back`
    days has a file, so a long NSE gap is reported, not silent. The file
    must be for the day that was asked for."""
    for back in range(days_back + 1):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        data = _get(NSE_URL.format(ymd=day.strftime("%Y%m%d")))
        if data is not None:
            file = parse_bhavcopy(data, today)
            if file.day != day:
                raise EodRejected(f"NSE's bhavcopy for {day} is dated {file.day}")
            return file
    raise EodRejected(f"no NSE bhavcopy found in the {days_back} days before {today}")


def only(file: PriceFile, isins: frozenset[Isin]) -> PriceFile:
    """Keep the ISINs you hold. prices.db stays small, and a holding you
    add later is priced from the next fetch."""
    return file.model_copy(update={"quotes": tuple(q for q in file.quotes if q.isin in isins)})
