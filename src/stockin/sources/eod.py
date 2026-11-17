"""Public end-of-day price files: parse, and fetch.

- AMFI `NAVAll.txt`: every mutual fund scheme's latest NAV. Semicolon
  separated: Scheme Code; ISIN Div Payout/ISIN Growth; ISIN Div
  Reinvestment; Scheme Name; Plan; Option; Net Asset Value; Date. Older
  files have no Plan and Option columns, so the NAV and date are read from
  the end of the row. Section headings and blank lines have no semicolons.
- NSE cash-market bhavcopy (the UDiFF CSV, usually zipped): one row per
  instrument and series. ISIN, SctySrs and ClsPric are read by header name.

Parsers are pure functions of the file's bytes. `fetch_*` are the only
network calls, and they only download public files: no login, no token.
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
from collections.abc import Iterable
from datetime import date, datetime, timedelta

from ..facts import Isin, parse_isin
from ..money import Price
from ..prices import Quote

AMFI_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
NSE_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
_UA = "Mozilla/5.0 (stockin; personal read-only price download)"

_SERIES_RANK = {"EQ": 0, "BE": 1, "BZ": 2, "SM": 3, "ST": 4}
"""One ISIN can trade in several series. The normal-market series wins."""


class EodRejected(ValueError):
    """The file is not a price file this module understands."""


def _isin(text: str) -> Isin | None:
    try:
        return parse_isin(text)
    except ValueError:
        return None


def _price(text: str) -> Price | None:
    try:
        price = Price.rupees(text)
    except ValueError:
        return None
    return price


def parse_amfi(text: str) -> tuple[Quote, ...]:
    """Every (ISIN, NAV, date) in a NAVAll file. Rows whose NAV is not a
    positive number (e.g. "N.A.") or whose ISIN is "-" are skipped. Both ISIN
    columns get the NAV: payout/growth and reinvestment units share it."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("Scheme Code;"):
        raise EodRejected("not an AMFI NAVAll file (first line is not its header)")
    out: dict[Isin, Quote] = {}
    for line in lines[1:]:
        cells = [c.strip() for c in line.split(";")]
        if len(cells) < 6:
            continue
        nav, on_text = cells[-2], cells[-1]
        try:
            on = datetime.strptime(on_text, "%d-%b-%Y").date()
        except ValueError:
            continue
        price = _price(nav)
        if price is None:
            continue
        for cell in (cells[1], cells[2]):
            isin = _isin(cell)
            if isin is None:
                continue
            quote = Quote(isin=isin, on=on, price=price, source="amfi")
            prev = out.get(isin)
            if prev is None or (quote.on, quote.price.micro_rupees) > (
                prev.on,
                prev.price.micro_rupees,
            ):
                out[isin] = quote
    if not out:
        raise EodRejected("the NAVAll file has no NAV rows")
    return tuple(out[k] for k in sorted(out))


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
        raise EodRejected("the bhavcopy is not UTF-8 text") from None
    except EodRejected:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError, RuntimeError) as exc:
        # zipfile reports a damaged archive as any of these (a flipped
        # "encrypted" bit is RuntimeError, a bad directory offset ValueError)
        raise EodRejected(f"the bhavcopy zip is damaged or incomplete ({exc})") from None


def parse_bhavcopy(data: bytes) -> tuple[Quote, ...]:
    """Close price per ISIN from an NSE UDiFF cash-market bhavcopy (zip or
    CSV). Prefers the EQ series when an ISIN trades in several. A CSV the
    csv module cannot read (a stray quote, a bare CR) is rejected."""
    text = _csv_text(data)
    try:
        return _bhavcopy_rows(text)
    except csv.Error as exc:
        raise EodRejected(f"the bhavcopy CSV is damaged ({exc})") from None


def _bhavcopy_rows(text: str) -> tuple[Quote, ...]:
    reader = csv.DictReader(io.StringIO(text))
    need = {"TradDt", "ISIN", "SctySrs", "ClsPric"}
    if reader.fieldnames is None or not need <= set(reader.fieldnames):
        raise EodRejected("not an NSE UDiFF bhavcopy (missing TradDt/ISIN/SctySrs/ClsPric)")
    best: dict[Isin, tuple[int, Quote]] = {}
    for row in reader:
        isin = _isin(row["ISIN"] or "")
        price = _price(row["ClsPric"] or "")
        if isin is None or price is None:
            continue
        try:
            on = date.fromisoformat(row["TradDt"].strip())
        except ValueError:
            continue
        rank = _SERIES_RANK.get((row["SctySrs"] or "").strip(), 9)
        quote = Quote(isin=isin, on=on, price=price, source="bhavcopy")
        prev = best.get(isin)
        key = (rank, -quote.price.micro_rupees)
        if prev is None or key < (prev[0], -prev[1].price.micro_rupees):
            best[isin] = (rank, quote)
    if not best:
        raise EodRejected("the bhavcopy has no price rows")
    return tuple(best[k][1] for k in sorted(best))


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


def fetch_amfi() -> tuple[Quote, ...]:
    data = _get(AMFI_URL)
    if data is None:
        raise EodRejected("AMFI NAVAll.txt was not found")
    return parse_amfi(data.decode("utf-8-sig", errors="replace"))


def fetch_bhavcopy(today: date, days_back: int = 7) -> tuple[Quote, ...]:
    """The newest bhavcopy on or before `today`, looking back over weekends
    (not asked for) and holidays (404). Rejected if no weekday in `days_back`
    days has a file, so a long NSE gap is reported, not silent."""
    for back in range(days_back + 1):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        data = _get(NSE_URL.format(ymd=day.strftime("%Y%m%d")))
        if data is not None:
            return parse_bhavcopy(data)
    raise EodRejected(f"no NSE bhavcopy found in the {days_back} days before {today}")


def only(quotes: Iterable[Quote], isins: frozenset[Isin]) -> tuple[Quote, ...]:
    """Keep the ISINs you hold. prices.db stays small, and a holding you
    add later is priced from the next fetch."""
    return tuple(q for q in quotes if q.isin in isins)
