"""End-of-day prices (design/ledger/sketch/prices.py).

Prices are public, reproducible data, so they are NOT ledger facts. They live
in their own SQLite file, prices.db, next to ledger.db. Only the price job
(`stockin prices fetch|import`) writes it, and only the CLI writes the
ledger. Two writers, two files, no shared rows. Pure code sees prices only as
a `PriceBook` argument.

prices.db keeps what each price file said: one row per (isin, source, file
day). A plain CSV or NAVAll has no checksum, so a damaged copy can carry a
wrong price that no check can see. The rule that makes that harmless:
importing a file REPLACES what is stored for its source and day, and readers
take each source's newest file. So the last import of a day wins, the same
file twice changes nothing, and importing the right file undoes a damaged
one completely. Older days stay as history.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
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

OLDEST_FILE_DAY = date(1994, 1, 1)
"""No price file is dated before NSE's first trading year. Checked where a
file is read (both parsers) and where its rows are kept (the scan on open),
so a file dated 0001-01-01 is refused, not stored (verifier 3 on #21)."""

OLDEST_NAV_YEARS = 30
"""No real NAV is more than 30 calendar years older than its file's day (see
eod.AMFI_OLDEST_YEARS). One number, checked where a file is read (the
parser) and where its rows are kept (the scan on open)."""


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


class PriceFile(Value):
    """What one price file says: one quote per ISIN, and the day the file is
    for. A bhavcopy's day is its trading date. A NAVAll's day is the date
    most of its rows carry (a scheme's last NAV can be older, or one evening
    newer while AMFI is still updating)."""

    source: Source
    day: date
    quotes: tuple[Quote, ...]


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


JUMP_FACTOR = 2
"""A price more than double, or less than half, the last one from an earlier
day is held for the owner to confirm. NSE's widest daily band is 20% and a
fund's NAV moves a few percent, so no ordinary day crosses 2x; a split or a
1:1 bonus does, and so does a lost decimal point or a damaged leading digit.
A real split needs a look anyway: units come from the last statement."""


class Jump(Value):
    """A quote held out of prices.db: `quote` is what the file says, `last`
    the price it was compared with."""

    quote: Quote
    last: Quote


def is_jump(last: Price, new: Price) -> bool:
    a, b = last.micro_rupees, new.micro_rupees
    return b > JUMP_FACTOR * a or JUMP_FACTOR * b < a


def hold_jumps(
    file: PriceFile, last: PriceBook, stored: Mapping[Isin, Quote]
) -> tuple[PriceFile, tuple[Jump, ...]]:
    """Split `file` into what may be stored and the jumps held back. `last`
    is the book from earlier days; `stored` is what prices.db already has for
    this file's source and day. A quote that is already stored was accepted
    before and is not held again. A held ISIN gets no price for the day (not
    even the one stored before), so what a file leaves behind never depends
    on a damaged copy imported before it."""
    keep: list[Quote] = []
    held: list[Jump] = []
    for q in file.quotes:
        prev = last.get(q.isin)
        if prev is None or q == stored.get(q.isin) or not is_jump(prev.price, q.price):
            keep.append(q)
        else:
            held.append(Jump(quote=q, last=prev))
    return file.model_copy(update={"quotes": tuple(keep)}), tuple(held)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes(
  isin TEXT NOT NULL, source TEXT NOT NULL, file_day TEXT NOT NULL,
  on_date TEXT NOT NULL, micro_rupees INTEGER NOT NULL CHECK (micro_rupees > 0),
  PRIMARY KEY(isin, source, file_day)) WITHOUT ROWID
"""


class PricesMissing(FileNotFoundError):
    """Raised by a read handle when no prices have been fetched yet."""


class PricesDamaged(ValueError):
    """prices.db exists but is not a Stockin price file (or not SQLite)."""


_COLUMNS = (  # (name, primary-key position)
    ("isin", 1),
    ("source", 2),
    ("file_day", 3),
    ("on_date", 0),
    ("micro_rupees", 0),
)


_ISIN_GLOB = "[A-Z][A-Z]" + "[A-Z0-9]" * 9 + "[0-9]"
"""An ISIN's shape: country, nine letters or digits, a check digit."""

_OUR_TABLE = (
    "table",
    "quotes",
    " ".join(_SCHEMA.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE").split()),
)
"""sqlite_master's row for the table `_SCHEMA` creates (SQLite keeps the
statement's text, without IF NOT EXISTS), whitespace aside."""

_IST = timezone(timedelta(hours=5, minutes=30))


def india_day(now: datetime) -> date:
    return now.astimezone(_IST).date()


def _now() -> datetime:
    """The clock, in one place so tests can move it."""
    return datetime.now(_IST)


def india_today() -> date:
    """Today in India, where both price files are dated. Nothing in
    prices.db may be dated after it."""
    return india_day(_now())


def _check_shape(conn: sqlite3.Connection, path: Path) -> bool:
    """True if prices.db is ours and intact; False if the file has no tables
    at all (new or 0 bytes). Anything else is PricesDamaged: a file another
    program (or an older Stockin) made, a damaged page, or a row we could not
    have written. Never guess at, or write into, such a file."""
    objects = conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
    if not objects:
        return False
    # The file must hold exactly the table Stockin creates, constraints and
    # all: a table with our column names but no NOT NULL let a NULL date past
    # the scan and crash every read (verifier 3 on #21). A file with any other
    # table, index, view or trigger is not ours either.
    if [(t, n, " ".join(str(q).split())) for t, n, q in objects] != [_OUR_TABLE]:
        raise PricesDamaged(f"{path} is not a Stockin price file (unexpected tables)")
    # SQLite pages have no checksum. integrity_check (not quick_check) also
    # finds a key byte that puts rows out of key order, which leaves lookups
    # returning a stale row while every value still looks valid (verifier 7).
    problems = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    if problems != ["ok"]:
        raise PricesDamaged(f"{path} is damaged ({problems[0]})")
    # a row dated after today would win "newest file" for ever: no later
    # import could replace it (verifier 6)
    today = india_today().isoformat()
    cols = tuple((r[1], r[5]) for r in conn.execute("PRAGMA table_info(quotes)"))
    if cols != _COLUMNS:
        raise PricesDamaged(f"{path} is not a Stockin price file (unexpected tables)")
    # A full scan: a damaged page anywhere in the table raises here. date()
    # is NULL for text that is not a real day (2026-13-39) and normalises an
    # impossible one (2026-02-30), so `IS NOT date(x)` is a real date check.
    # But SQLite has a year 0000 and Python's date does not: such a row passed
    # the scan and then crashed the read (verifier 2 on #21). No file is
    # dated before OLDEST_FILE_DAY and no NAV is more than OLDEST_NAV_YEARS
    # older than its file (the parsers' own bounds; by calendar year, like
    # the NAVAll parser), so every date a row may hold is one Python can.
    odd = conn.execute(
        """SELECT 1 FROM quotes WHERE typeof(isin) != 'text' OR isin NOT GLOB ?
             OR source NOT IN ('bhavcopy', 'amfi')
             OR file_day IS NOT date(file_day) OR on_date IS NOT date(on_date)
             OR (source = 'bhavcopy' AND on_date != file_day)
             OR file_day > ? OR on_date > ?
             OR file_day < ?
             OR CAST(substr(on_date, 1, 4) AS INTEGER)
                < CAST(substr(file_day, 1, 4) AS INTEGER) - ?
             OR typeof(micro_rupees) != 'integer'
             OR micro_rupees <= 0 OR micro_rupees > ? LIMIT 1""",
        (
            _ISIN_GLOB,
            today,
            today,
            OLDEST_FILE_DAY.isoformat(),
            OLDEST_NAV_YEARS,
            MAX_PRICE_MICROS,
        ),
    ).fetchone()
    if odd is not None:
        raise PricesDamaged(f"{path} holds rows Stockin could not have written")
    return True


@contextmanager
def _guard(path: Path) -> Generator[None]:
    """SQLite failing on the file (not a database, a damaged page, or it
    changed under us) is PricesDamaged, never a traceback."""
    try:
        yield
    except PricesDamaged:
        raise  # already says what is wrong (it is a ValueError too)
    except (sqlite3.DatabaseError, ValueError) as exc:
        # ValueError: a stored value Python cannot hold, whatever the scan on
        # open let through or was changed after it. That includes
        # UnicodeDecodeError, a damaged byte in text SQLite hands back, such
        # as the table's own schema (verifier 7).
        raise PricesDamaged(f"{path} is not a Stockin price file ({exc})") from None


class PriceStore:
    """prices.db. `book()` for readers; `replace()` only for the price job."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self._path = path

    @classmethod
    def open(cls, path: Path, *, write: bool = False) -> Self:
        if not path.exists() and not path.is_symlink():
            if not write:
                raise PricesMissing(f"no prices at {path}; run `stockin prices fetch`")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600)  # which ISINs you hold is private, like ledger.db
        if not path.is_file():
            raise PricesDamaged(f"{path} is not a file")
        with _guard(path):
            conn = sqlite3.connect(path)
            try:
                if not write:
                    conn.execute("PRAGMA query_only=ON")
                ours = _check_shape(conn, path)
                if write:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(_SCHEMA)
            except (sqlite3.DatabaseError, UnicodeDecodeError, PricesDamaged):
                conn.close()
                raise
        if not ours and not write:  # an empty file: nothing fetched yet
            conn.close()
            raise PricesMissing(f"{path} holds no prices; run `stockin prices fetch`")
        return cls(conn, path)

    def stored(self, source: Source, day: date) -> dict[Isin, Quote]:
        """What prices.db holds for one source's file of one day."""
        with _guard(self._path):
            return self._stored(source, day)

    def _stored(self, source: Source, day: date) -> dict[Isin, Quote]:
        rows = self._conn.execute(
            "SELECT isin, on_date, micro_rupees FROM quotes WHERE source=? AND file_day=?",
            (source, day.isoformat()),
        ).fetchall()
        return {Isin(i): _quote(Isin(i), on, source, micros) for i, on, micros in rows}

    def replace(self, file: PriceFile) -> int:
        """Make prices.db say, for this file's source and day, exactly what
        `file` says. Returns the rows added, changed or removed."""
        new = {q.isin: q for q in file.quotes}
        day = file.day.isoformat()
        with _guard(self._path), self._conn:
            old = self._stored(file.source, file.day)
            if new != old:
                self._conn.execute(
                    "DELETE FROM quotes WHERE source=? AND file_day=?", (file.source, day)
                )
                self._conn.executemany(
                    "INSERT INTO quotes VALUES (?,?,?,?,?)",
                    [
                        (q.isin, file.source, day, q.on.isoformat(), q.price.micro_rupees)
                        for q in new.values()
                    ],
                )
        return sum(1 for isin in new.keys() | old.keys() if new.get(isin) != old.get(isin))

    def take(self, file: PriceFile, accept_jumps: bool = False) -> tuple[int, tuple[Jump, ...]]:
        """Import one file: `replace`, with any price that jumped from the
        last earlier one held back unless `accept_jumps`. Returns the rows
        changed and the jumps held."""
        held: tuple[Jump, ...] = ()
        if not accept_jumps:
            last = self.book((q.isin for q in file.quotes), file.day - timedelta(days=1))
            file, held = hold_jumps(file, last, self.stored(file.source, file.day))
        return self.replace(file), held

    def book(self, isins: Iterable[Isin], on_or_before: date | None = None) -> PriceBook:
        """The best quote per ISIN: each source's newest file that prices it
        (file day on or before `on_or_before`; default any), then
        `PriceBook.best` between the sources."""
        sql = "SELECT on_date, micro_rupees FROM quotes WHERE isin=? AND source=?"
        tail: tuple[str, ...] = ()
        if on_or_before is not None:
            sql += " AND file_day <= ?"
            tail = (on_or_before.isoformat(),)
        sql += " ORDER BY file_day DESC LIMIT 1"
        quotes: list[Quote] = []
        with _guard(self._path):
            for isin in sorted(set(isins)):
                for source in _RANK:
                    for on, micros in self._conn.execute(sql, (isin, source, *tail)):
                        quotes.append(_quote(isin, on, source, micros))
        return PriceBook.best(quotes)

    def latest_day(self) -> date | None:
        """The day of the newest file stored."""
        with _guard(self._path):
            (day,) = self._conn.execute("SELECT max(file_day) FROM quotes").fetchone()
            return None if day is None else date.fromisoformat(day)

    def count(self) -> int:
        with _guard(self._path):
            (n,) = self._conn.execute("SELECT count(*) FROM quotes").fetchone()
        return int(n)

    def digest(self) -> str:
        h = hashlib.sha256()
        for row in self._conn.execute("SELECT * FROM quotes ORDER BY isin, source, file_day"):
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


def _quote(isin: Isin, on: str, source: str, micros: int) -> Quote:
    return Quote(
        isin=isin,
        on=date.fromisoformat(on),
        price=Price(micro_rupees=micros),
        source=source,  # pyright: ignore[reportArgumentType]  (the open-time scan checked it)
    )
