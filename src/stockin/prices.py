"""End-of-day prices (design/ledger/sketch/prices.py).

Prices are public, reproducible data, so they are NOT ledger facts. They live
in their own SQLite file, prices.db, next to ledger.db. Only the price job
(`stockin prices fetch|import`) writes it, and only the CLI writes the
ledger. Two writers, two files, no shared rows. Pure code sees prices only as
a `PriceBook` argument.

One row per (isin, on, source). Writing the same file twice changes nothing,
and the database depends on the set of files written, not their order, as
long as no two files disagree about the same (isin, on, source). If they do,
the larger price is kept, which is arbitrary but order-independent.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from pydantic import model_validator

from .facts import Isin
from .money import Money, Price, Qty, Value

Source = Literal["bhavcopy", "amfi"]
_RANK: dict[str, int] = {"bhavcopy": 0, "amfi": 1}
"""Tie-break on the same day: the exchange close (what you could sell at)
beats the AMFI NAV. Only an ETF has both."""


MAX_PRICE_RUPEES = 10**7
"""₹1 crore a unit. The dearest Indian share and the largest NAV are far
below it; a price above it can only come from a damaged file, and it would
not fit SQLite's integer as micro-rupees much further up."""
MAX_PRICE_MICROS = MAX_PRICE_RUPEES * 10**6


class Quote(Value):
    """The close of one instrument on one trading day, from one source."""

    isin: Isin
    on: date
    price: Price
    source: Source

    @model_validator(mode="after")
    def _real_price(self) -> Self:
        if self.price.micro_rupees > MAX_PRICE_MICROS:
            raise ValueError(f"{self.isin}: price above ₹{MAX_PRICE_RUPEES:,} a unit")
        return self


class PriceBook(Value):
    """Best known quote per ISIN. A missing ISIN is unpriced: callers get
    None, never zero. The quote's date travels with it, so staleness is
    always visible."""

    quotes: Mapping[Isin, Quote]

    def get(self, isin: Isin) -> Quote | None:
        return self.quotes.get(isin)

    def value(self, isin: Isin, qty: Qty) -> Money | None:
        quote = self.get(isin)
        return None if quote is None else Money.of(qty, quote.price)

    @classmethod
    def best(cls, quotes: Iterable[Quote]) -> PriceBook:
        """Latest date per ISIN; on a tie, bhavcopy before amfi. Pure, and
        independent of input order."""
        out: dict[Isin, Quote] = {}
        for q in quotes:
            cur = out.get(q.isin)
            if cur is None or _better(q, cur):
                out[q.isin] = q
        return cls(quotes=out)


def _better(a: Quote, b: Quote) -> bool:
    if a.on != b.on:
        return a.on > b.on
    if a.source != b.source:
        return _RANK[a.source] < _RANK[b.source]
    return a.price.micro_rupees > b.price.micro_rupees


_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes(
  isin TEXT NOT NULL, on_date TEXT NOT NULL, source TEXT NOT NULL,
  micro_rupees INTEGER NOT NULL CHECK (micro_rupees > 0),
  PRIMARY KEY(isin, on_date, source)) WITHOUT ROWID;
PRAGMA user_version = 1;
"""


class PricesMissing(FileNotFoundError):
    """Raised by a read handle when no prices have been fetched yet."""


class PricesDamaged(ValueError):
    """prices.db exists but is not a Stockin price file (or not SQLite)."""


_COLUMNS = (("isin", 1), ("on_date", 2), ("source", 3), ("micro_rupees", 0))  # (name, pk position)


def _check_shape(conn: sqlite3.Connection, path: Path) -> bool:
    """True if prices.db is ours and intact; False if the file has no tables
    at all (new or 0 bytes). Anything else is PricesDamaged: a file another
    program made (even with our column names), a damaged page, or a row we
    could not have written. Never guess at, or write into, such a file."""
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if not tables:
            return False
        cols = tuple((r[1], r[5]) for r in conn.execute("PRAGMA table_info(quotes)"))
        if cols != _COLUMNS:
            raise PricesDamaged(f"{path} is not a Stockin price file (unexpected tables)")
        # a full scan: a damaged page anywhere in the table raises here
        odd = conn.execute(
            """SELECT 1 FROM quotes WHERE typeof(isin) != 'text' OR length(isin) != 12
                 OR typeof(on_date) != 'text'
                 OR on_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'
                 OR source NOT IN ('bhavcopy', 'amfi') OR typeof(micro_rupees) != 'integer'
                 OR micro_rupees <= 0 OR micro_rupees > ? LIMIT 1""",
            (MAX_PRICE_MICROS,),
        ).fetchone()
        if odd is not None:
            raise PricesDamaged(f"{path} holds rows Stockin could not have written")
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise PricesDamaged(f"{path} is not a Stockin price file ({exc})") from None
    except PricesDamaged:
        conn.close()
        raise
    return True


@contextmanager
def _guard(path: Path) -> Generator[None]:
    """A read or write that still fails on the file (it changed under us, or
    a date in it does not parse) is PricesDamaged, never a traceback."""
    try:
        yield
    except (sqlite3.DatabaseError, ValueError) as exc:
        if isinstance(exc, PricesDamaged):
            raise
        raise PricesDamaged(f"{path} could not be read as a Stockin price file ({exc})") from None


class PriceStore:
    """prices.db. `book()` for readers; `put()` only for the price job."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self._path = path

    @classmethod
    def open(cls, path: Path, *, write: bool = False) -> Self:
        if not write:
            if not path.is_file():
                raise PricesMissing(f"no prices at {path}; run `stockin prices fetch`")
            with _guard(path):
                conn = sqlite3.connect(path)
                conn.execute("PRAGMA query_only=ON")
            if not _check_shape(conn, path):  # an empty file: nothing fetched yet
                conn.close()
                raise PricesMissing(f"{path} holds no prices; run `stockin prices fetch`")
            return cls(conn, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        with _guard(path):
            conn = sqlite3.connect(path, isolation_level=None)
        if fresh:
            path.chmod(0o600)  # which ISINs you hold is private, like ledger.db
        _check_shape(conn, path)
        with _guard(path):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        conn.isolation_level = "DEFERRED"
        return cls(conn, path)

    def put(self, quotes: Iterable[Quote]) -> int:
        """Upsert keyed (isin, on, source). Returns rows added or changed."""
        changed = 0
        with _guard(self._path), self._conn:
            for q in quotes:
                cur = self._conn.execute(
                    """INSERT INTO quotes VALUES (?,?,?,?)
                       ON CONFLICT(isin, on_date, source) DO UPDATE
                       SET micro_rupees = excluded.micro_rupees
                       WHERE excluded.micro_rupees > quotes.micro_rupees""",
                    (q.isin, q.on.isoformat(), q.source, q.price.micro_rupees),
                )
                changed += cur.rowcount
        return changed

    def book(self, isins: Iterable[Isin], on_or_before: date | None = None) -> PriceBook:
        """The best quote per ISIN on or before `on_or_before` (default: any)."""
        wanted = sorted(set(isins))
        quotes: list[Quote] = []
        with _guard(self._path):
            for isin in wanted:
                quotes.extend(self._quotes(isin, on_or_before))
        return PriceBook.best(quotes)

    def _quotes(self, isin: Isin, on_or_before: date | None) -> list[Quote]:
        quotes: list[Quote] = []
        sql = "SELECT on_date, source, micro_rupees FROM quotes WHERE isin=?"
        args: tuple[str, ...] = (isin,)
        if on_or_before is not None:
            sql += " AND on_date <= ?"
            args += (on_or_before.isoformat(),)
        sql += " ORDER BY on_date DESC LIMIT 2"
        for on, source, micros in self._conn.execute(sql, args):
            quotes.append(
                Quote(
                    isin=isin,
                    on=date.fromisoformat(on),
                    price=Price(micro_rupees=int(micros)),
                    source=source,
                )
            )
        return quotes

    def latest_day(self) -> date | None:
        with _guard(self._path):
            (on,) = self._conn.execute("SELECT max(on_date) FROM quotes").fetchone()
            return None if on is None else date.fromisoformat(on)

    def count(self) -> int:
        with _guard(self._path):
            (n,) = self._conn.execute("SELECT count(*) FROM quotes").fetchone()
            return int(n)

    def digest(self) -> str:
        h = hashlib.sha256()
        for row in self._conn.execute("SELECT * FROM quotes ORDER BY isin, on_date, source"):
            h.update(repr(tuple(row)).encode())
            h.update(b"\n")
        return h.hexdigest()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
