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

A price held back as a jump is not stored as a price, but what the file
said is kept beside the prices (table `held`, same key), replaced with its
file like them. Nothing is valued from it: it tells readers that the price
still in use is in doubt, so no verdict acts on it (PriceBook.doubted).
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
    always visible.

    `held` is the newest price a file gave for an ISIN that was held back
    and not stored (see is_jump). It is never a price to value anything
    with; it is there so a reader can tell that the price in use is in
    doubt (`doubted`)."""

    quotes: Mapping[Isin, Quote]
    held: Mapping[Isin, Quote] = {}

    def get(self, isin: Isin) -> Quote | None:
        return self.quotes.get(isin)

    def doubted(self, isin: Isin) -> Quote | None:
        """The held-back price that puts this ISIN's price in doubt: one at
        least as new as the price in use and still more than the band away
        from it. None once a price as new has been stored (--accept-jumps,
        or a later file back within the band), or once the price in use
        agrees with the held one (an older day's file accepted while a
        later day's like price is still held; #39 verify-3 C-1). A held
        price was not stored, so the price in use can be months old and
        wrong by any amount; no add or trim may be worked out from it
        (#39 verifier round 2, R-1)."""
        held, used = self.held.get(isin), self.quotes.get(isin)
        if held is None or used is None:
            return held
        if used.on > held.on or not beyond_band(used.price, held.price):
            return None
        return held

    def value(self, isin: Isin, qty: Qty) -> Money | None:
        quote = self.get(isin)
        return None if quote is None else Money.of(qty, quote.price)

    @classmethod
    def best(cls, quotes: Iterable[Quote], held: Iterable[Quote] = ()) -> PriceBook:
        """Latest date per ISIN; on a tie, bhavcopy before amfi. Pure, and
        independent of input order. `held` is picked the same way."""
        return cls(quotes=_best(quotes), held=_best(held))


def _best(quotes: Iterable[Quote]) -> dict[Isin, Quote]:
    out: dict[Isin, Quote] = {}
    for q in quotes:
        cur = out.get(q.isin)
        if cur is None or _better(q, cur):
            out[q.isin] = q
    return out


def rupee_text(price: Price) -> str:
    """Exact, at least two decimals: ₹1,367.00, ₹2,378.851, ₹94.4817."""
    whole, micros = divmod(price.micro_rupees, 1_000_000)
    frac = f"{micros:06d}".rstrip("0").ljust(2, "0")
    return "\u20b9" + str(Money(paise=whole * 100))[:-3] + "." + frac


def _better(a: Quote, b: Quote) -> bool:
    if a.on != b.on:
        return a.on > b.on
    if a.source != b.source:
        return _RANK[a.source] < _RANK[b.source]
    return a.price.micro_rupees > b.price.micro_rupees


JUMP_FACTOR = 2
"""A price more than double, or less than half, the last one from an earlier
day is held for the owner to confirm, however old that last one is. A lost
decimal point or a damaged leading digit crosses 2x, and so does a 1:1 split
or bonus. A real split needs a look anyway: units come from the last statement."""

BAND_BPS = 2_000
BAND_DAYS = 7
"""A price more than 20% away from the last one of the same source is held
too, however old that one is (see is_jump). NSE's widest daily price band
is 20%, so a share that moved further between two files is rare, and a 1:1
split whose ex-date close sits just above half the old close (₹750.30
against ₹1,500.55) is caught here, not let through by the 2x rule (#33
verifier 3, R-1). BAND_DAYS is how old a price may be and still be acted on:
no add or trim is worked out from an older one (thesis.for_book)."""


class Jump(Value):
    """A quote held out of prices.db: `quote` is what the file says, `last`
    the price it was compared with."""

    quote: Quote
    last: Quote


def beyond_band(last: Price, new: Price, band_bps: int = BAND_BPS) -> bool:
    """`new` more than `band_bps` away from `last`, exactly in integers."""
    a, b = last.micro_rupees, new.micro_rupees
    return abs(b - a) * 10_000 > band_bps * a


def is_jump(last: Quote, new: Quote) -> bool:
    """Held for the owner: beyond 2x from any earlier price, or beyond 20%
    from the last price of the same source, however old.

    The band compares like with like: a fund's NAV and its exchange close
    can differ legitimately, so AMFI against bhavcopy is held only at 2x.
    It has no age limit, because a held price is not stored: with one, a
    split held every evening was let in once the last stored close turned
    a week old, and the next send could carry a wrong ADD (#39 verifier R-1).
    After a long gap a real move beyond 20% is held too (a share sold and
    bought back months later, a long break without fetches), and the owner
    accepts it once with --accept-jumps. Until then the old price stays in
    use, so what is held is kept (PriceStore.take) and readers treat the
    price in use as in doubt (PriceBook.doubted; #39 verifier round 2)."""
    a, b = last.price.micro_rupees, new.price.micro_rupees
    if b > JUMP_FACTOR * a or JUMP_FACTOR * b < a:
        return True
    return last.source == new.source and beyond_band(last.price, new.price)


def hold_jumps(
    file: PriceFile,
    last: PriceBook,
    stored: Mapping[Isin, Quote],
    same: PriceBook | None = None,
) -> tuple[PriceFile, tuple[Jump, ...]]:
    """Split `file` into what may be stored and the jumps held back. `last`
    is the book from earlier days; `same` is that book from the file's own
    source only, so the 20% band still sees the last NAV when a bhavcopy
    close stands in front of it in `last` (or the other way round, #38
    verifier R-1). `stored` is what prices.db already has for this file's
    source and day. A quote that is already stored was accepted
    before and is not held again. A held ISIN gets no price for the day (not
    even the one stored before), so what a file leaves behind never depends
    on a damaged copy imported before it."""
    keep: list[Quote] = []
    held: list[Jump] = []
    for q in file.quotes:
        prevs = [p for p in (last.get(q.isin), same.get(q.isin) if same else None) if p]
        jumped = next((p for p in prevs if is_jump(p, q)), None)
        if jumped is None or q == stored.get(q.isin):
            keep.append(q)
        else:
            held.append(Jump(quote=q, last=jumped))
    return file.model_copy(update={"quotes": tuple(keep)}), tuple(held)


def left_out(
    file: PriceFile, stored: Mapping[Isin, Quote], held: frozenset[Isin]
) -> tuple[Quote, ...]:
    """The prices importing `file` removes: stored for its source and day,
    for a fund you hold, and not listed in it. Two things look exactly like
    this and nothing in the file tells them apart. A file cut short (a
    NAVAll download or copy that stops at a line end still parses) lists
    fewer funds than the whole one imported before it (verifier 2 on #22).
    And the right file, after a damaged copy stored a price for a fund this
    source never prices (verifier 3 on #22). So the file is always stored as
    it is, which keeps "the right file undoes any damaged copy" true without
    exception, and what it removed is returned to be reported, never kept:
    a kept price is one the right file cannot take back."""
    listed = {q.isin for q in file.quotes}
    return tuple(q for isin, q in sorted(stored.items()) if isin in held and isin not in listed)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes(
  isin TEXT NOT NULL, source TEXT NOT NULL, file_day TEXT NOT NULL,
  on_date TEXT NOT NULL, micro_rupees INTEGER NOT NULL CHECK (micro_rupees > 0),
  PRIMARY KEY(isin, source, file_day)) WITHOUT ROWID
"""


_HELD_SCHEMA = _SCHEMA.replace("quotes(", "held(")
"""What a file said and Stockin did not store: the prices held back as
jumps, one row per (isin, source, file day) like `quotes`. Importing a file
replaces its source and day here too, so the same file leaves the same rows
and --accept-jumps (or the right file) clears them. Nothing is valued from
this table; readers use it to know a price is in doubt (PriceBook.doubted)."""
_TABLES = ("quotes", "held")


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

_OUR_TABLES = [
    (
        "table",
        name,
        " ".join(sql.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE").split()),
    )
    for name, sql in zip(_TABLES, (_SCHEMA, _HELD_SCHEMA), strict=True)
]
"""sqlite_master's rows for the tables `_SCHEMA` and `_HELD_SCHEMA` create
(SQLite keeps the statement's text, without IF NOT EXISTS), whitespace
aside. A prices.db written before `held` existed has only the first."""

_IST = timezone(timedelta(hours=5, minutes=30))


def india_day(now: datetime) -> date:
    return now.astimezone(_IST).date()


def _now() -> datetime:
    """The clock, in one place so tests can move it."""
    return datetime.now(_IST)


def india_now() -> datetime:
    """Now in India, to the second."""
    return _now().replace(microsecond=0)


def india_today() -> date:
    """Today in India, where both price files are dated. Nothing in
    prices.db may be dated after it."""
    return india_day(_now())


def _check_shape(conn: sqlite3.Connection, path: Path) -> int:
    """How many of our tables prices.db holds, once it is known to be ours
    and intact: 2, or 1 for a file from before `held` existed; 0 if the
    file has no tables at all (new or 0 bytes). Anything else is PricesDamaged: a file another
    program (or an older Stockin) made, a damaged page, or a row we could not
    have written. Never guess at, or write into, such a file."""
    objects = conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
    if not objects:
        # Only a file Stockin has not written to yet is empty: SQLite writes
        # nothing until the first table exists, and every write here creates
        # ours first. A file with bytes but no tables is a damaged header
        # (one zeroed byte at offset 104 reads as an empty schema), not a
        # new file to write into (verifier 2 on #22).
        if path.stat().st_size > 0:
            raise PricesDamaged(f"{path} is damaged (it has data but no tables)")
        return 0
    # The file must hold exactly the table Stockin creates, constraints and
    # all: a table with our column names but no NOT NULL let a NULL date past
    # the scan and crash every read (verifier 3 on #21). A file with any other
    # table, index, view or trigger is not ours either.
    found = [(t, n, " ".join(str(q).split())) for t, n, q in objects]
    if found not in (_OUR_TABLES[:1], _OUR_TABLES):
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
    for table in _TABLES[: len(found)]:
        cols = tuple((r[1], r[5]) for r in conn.execute(f"PRAGMA table_info({table})"))
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
    for table in _TABLES[: len(found)]:
        odd = conn.execute(
            f"""SELECT 1 FROM {table} WHERE typeof(isin) != 'text' OR isin NOT GLOB ?
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
    return len(found)


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

    def __init__(self, conn: sqlite3.Connection, path: Path, tables: int = 2) -> None:
        self._conn = conn
        self._path = path
        self._tables = _TABLES[:tables]  # a file from before `held` has one

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
                    conn.execute(_HELD_SCHEMA)
                    ours = len(_TABLES)
            except (sqlite3.DatabaseError, UnicodeDecodeError, PricesDamaged):
                conn.close()
                raise
        if not ours and not write:  # an empty file: nothing fetched yet
            conn.close()
            raise PricesMissing(f"{path} holds no prices; run `stockin prices fetch`")
        return cls(conn, path, ours)

    def stored(self, source: Source, day: date) -> dict[Isin, Quote]:
        """What prices.db holds for one source's file of one day."""
        with _guard(self._path):
            return self._stored(source, day)

    def _stored(self, source: Source, day: date, table: str = "quotes") -> dict[Isin, Quote]:
        rows = self._conn.execute(
            f"SELECT isin, on_date, micro_rupees FROM {table} WHERE source=? AND file_day=?",
            (source, day.isoformat()),
        ).fetchall()
        return {Isin(i): _quote(Isin(i), on, source, micros) for i, on, micros in rows}

    def replace(self, file: PriceFile, held: Iterable[Quote] = ()) -> int:
        """Make prices.db say, for this file's source and day, exactly what
        `file` says, and keep `held` as the prices of that file that were
        held back (none, unless `take` held some). Returns the price rows
        added, changed or removed."""
        new = {q.isin: q for q in file.quotes}
        out = {q.isin: q for q in held}
        with _guard(self._path), self._conn:
            old = self._stored(file.source, file.day)
            for table, want in (("quotes", new), ("held", out)):
                if want != self._stored(file.source, file.day, table):
                    self._put(table, file, want)
        return sum(1 for isin in new.keys() | old.keys() if new.get(isin) != old.get(isin))

    def _put(self, table: str, file: PriceFile, rows: Mapping[Isin, Quote]) -> None:
        day = file.day.isoformat()
        self._conn.execute(f"DELETE FROM {table} WHERE source=? AND file_day=?", (file.source, day))
        self._conn.executemany(
            f"INSERT INTO {table} VALUES (?,?,?,?,?)",
            [
                (q.isin, file.source, day, q.on.isoformat(), q.price.micro_rupees)
                for q in rows.values()
            ],
        )

    def take(self, file: PriceFile, accept_jumps: bool = False) -> tuple[int, tuple[Jump, ...]]:
        """Import one file: `replace`, with any price that jumped from the
        last earlier one held back unless `accept_jumps`. Returns the rows
        changed and the jumps held."""
        held: tuple[Jump, ...] = ()
        if not accept_jumps:
            isins = [q.isin for q in file.quotes]
            before = file.day - timedelta(days=1)
            last = self.book(isins, before)
            same = self.book(isins, before, sources=(file.source,))
            file, held = hold_jumps(file, last, self.stored(file.source, file.day), same)
        return self.replace(file, (j.quote for j in held)), held

    def book(
        self,
        isins: Iterable[Isin],
        on_or_before: date | None = None,
        sources: tuple[Source, ...] | None = None,
    ) -> PriceBook:
        """The best quote per ISIN: each source's newest file that prices it
        (file day on or before `on_or_before`; default any), then
        `PriceBook.best` between the sources (default all). The prices held
        back are read the same way into `PriceBook.held`."""
        tail: tuple[str, ...] = ()
        where = "WHERE isin=? AND source=?"
        if on_or_before is not None:
            where += " AND file_day <= ?"
            tail = (on_or_before.isoformat(),)
        found: dict[str, list[Quote]] = {table: [] for table in _TABLES}
        with _guard(self._path):
            for table in self._tables:
                sql = (
                    f"SELECT on_date, micro_rupees FROM {table} {where} "
                    "ORDER BY file_day DESC LIMIT 1"
                )
                for isin in sorted(set(isins)):
                    for source in _RANK if sources is None else sources:
                        for on, micros in self._conn.execute(sql, (isin, source, *tail)):
                            found[table].append(_quote(isin, on, source, micros))
        return PriceBook.best(found["quotes"], found["held"])

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
        for table in self._tables:
            tag = () if table == "quotes" else (table,)
            rows = self._conn.execute(f"SELECT * FROM {table} ORDER BY isin, source, file_day")
            for row in rows:
                h.update(repr((*row, *tag)).encode())
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
