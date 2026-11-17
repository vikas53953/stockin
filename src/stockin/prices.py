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
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from .facts import Isin
from .money import Money, Price, Qty, Value

Source = Literal["bhavcopy", "amfi"]
_RANK: dict[str, int] = {"bhavcopy": 0, "amfi": 1}
"""Tie-break on the same day: the exchange close (what you could sell at)
beats the AMFI NAV. Only an ETF has both."""


class Quote(Value):
    """The close of one instrument on one trading day, from one source."""

    isin: Isin
    on: date
    price: Price
    source: Source


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


class PriceStore:
    """prices.db. `book()` for readers; `put()` only for the price job."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path: Path, *, write: bool = False) -> Self:
        if not write:
            if not path.is_file():
                raise PricesMissing(f"no prices at {path}; run `stockin prices fetch`")
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA query_only=ON")
            return cls(conn)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.isolation_level = "DEFERRED"
        return cls(conn)

    def put(self, quotes: Iterable[Quote]) -> int:
        """Upsert keyed (isin, on, source). Returns rows added or changed."""
        changed = 0
        with self._conn:
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
        for isin in wanted:
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
        return PriceBook.best(quotes)

    def latest_day(self) -> date | None:
        (on,) = self._conn.execute("SELECT max(on_date) FROM quotes").fetchone()
        return None if on is None else date.fromisoformat(on)

    def count(self) -> int:
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
