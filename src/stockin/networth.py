"""Net worth: everything you own, minus what you owe, split by asset class.

Market holdings come from the ledger and end-of-day prices, exactly as the
home screen values them. Everything else (bank balances, fixed deposits,
EPF, PPF, NPS, gold, property, loans) you type in, and it lives in
assets.db beside ledger.db. Like theses.db it is the only copy of what you
wrote, so rebuilding the ledger never touches it, and it is private (0600).

Each entry is a dated snapshot: "on 31 Oct my EPF balance was 8,45,000".
The latest snapshot per (kind, label) is current; earlier ones are history,
kept so net worth can be drawn over time. Writing the same snapshot twice
changes nothing, and a second write for the same day replaces the first (it
is a correction). Closing an entry is a snapshot too, so nothing is deleted.

Max position weight is not measured here: add/trim/exit verdicts stay
measured against market holdings only (owner, 2026-11-17).

Every number is integer paise or basis points. The sentences under the
chart are built here from the same numbers the chart draws, the way
insights are.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, TypeAdapter, field_validator

from .book import Book
from .insights import pct, tenths_of, weights
from .money import Bps, Money, Price, Qty, Value, div_half_even
from .prices import PriceBook

# ------------------------------------------------------------------ kinds

Kind = Literal["bank", "fd", "epf", "ppf", "nps", "gold", "property", "other", "loan"]
StatedKind = Literal["bank", "epf", "ppf", "nps", "property", "other", "loan"]
Bucket = Literal["equity", "debt", "gold", "cash", "retirement", "real_estate", "other"]

KINDS: tuple[Kind, ...] = ("bank", "fd", "epf", "ppf", "nps", "gold", "property", "other", "loan")
BUCKETS: tuple[Bucket, ...] = (
    "equity",
    "debt",
    "gold",
    "cash",
    "retirement",
    "real_estate",
    "other",
)

KIND_BUCKET: Mapping[Kind, Bucket | None] = {
    "bank": "cash",
    "fd": "debt",
    "epf": "retirement",
    "ppf": "retirement",
    "nps": "retirement",
    "gold": "gold",
    "property": "real_estate",
    "other": "other",
    "loan": None,  # a liability: subtracted, never part of the mix
}

KIND_NAME: Mapping[Kind, str] = {
    "bank": "Bank",
    "fd": "FD",
    "epf": "EPF",
    "ppf": "PPF",
    "nps": "NPS",
    "gold": "Gold",
    "property": "Property",
    "other": "Other",
    "loan": "Loan",
}

BUCKET_NAME: Mapping[Bucket, str] = {
    "equity": "Equity",
    "debt": "Debt",
    "gold": "Gold",
    "cash": "Cash",
    "retirement": "Retirement",
    "real_estate": "Real estate",
    "other": "Other",
}

MAX_LABEL = 60
STALE_DAYS = 90
"""A typed balance older than this is flagged: passbooks move every month."""

DRIFT_BAND = Bps(500)
"""A class more than 5 percentage points from its target is called out, the
same band the add/trim verdicts use."""

MAX_RUPEES = 10**13
"""₹10 lakh crore. Anything larger is a typo, and stays far from overflow in
the basis-point and compounding arithmetic."""


FIRST_DAY = date(1900, 1, 1)
LAST_DAY = date(2199, 12, 31)
"""Dates outside these are typos; they also keep month arithmetic far from
the calendar's end (year 9999), where it would fail."""


def _in_range(d: date, what: str) -> date:
    if not FIRST_DAY <= d <= LAST_DAY:
        raise ValueError(f"{what} {d} is outside {FIRST_DAY.year} to {LAST_DAY.year}")
    return d


def label_key(label: str) -> str:
    """Identity of a label: spaces collapsed, case folded. 'SBI  FD' and
    'sbi fd' are the same entry; the label shown is the latest one typed."""
    return " ".join(label.split()).casefold()


Label = Annotated[str, Field(min_length=1, max_length=MAX_LABEL)]


class _Entry(Value):
    label: Label
    as_of: date

    @field_validator("label")
    @classmethod
    def _clean(cls, v: str) -> str:
        cleaned = " ".join(v.split())
        if not cleaned:
            raise ValueError("a label needs at least one letter or digit")
        if any(not ch.isprintable() for ch in cleaned):
            raise ValueError("a label cannot hold control characters")
        return cleaned

    @field_validator("as_of")
    @classmethod
    def _as_of(cls, v: date) -> date:
        return _in_range(v, "the date")


def _bounded(m: Money, what: str) -> Money:
    if m.paise < 0:
        raise ValueError(f"{what} cannot be negative")
    if m.paise > MAX_RUPEES * 100:
        raise ValueError(f"{what} is above ₹10 lakh crore; check the number")
    return m


class Stated(_Entry):
    """A balance read off a passbook, statement or valuation."""

    entry: Literal["stated"] = "stated"
    kind: StatedKind
    amount: Money

    @field_validator("amount")
    @classmethod
    def _amount(cls, v: Money) -> Money:
        return _bounded(v, "the amount")


class Fd(_Entry):
    """A fixed deposit: a contract, valued by formula on any day."""

    entry: Literal["fd"] = "fd"
    kind: Literal["fd"] = "fd"
    principal: Money
    rate: Bps  # 7.1% -> 710; above 50% is a typo
    start: date
    months: StrictInt = Field(gt=0, le=600)

    @field_validator("rate")
    @classmethod
    def _rate(cls, v: Bps) -> Bps:
        if not 0 <= v <= 5_000:
            raise ValueError("an interest rate above 50% is a typo; check the number")
        return v

    @field_validator("start")
    @classmethod
    def _start(cls, v: date) -> date:
        return _in_range(v, "the start date")

    @field_validator("principal")
    @classmethod
    def _principal(cls, v: Money) -> Money:
        if v.paise == 0:
            raise ValueError("the principal cannot be zero")
        return _bounded(v, "the principal")


class Gold(_Entry):
    """Physical gold: grams you hold, at a rate per gram you typed."""

    entry: Literal["gold"] = "gold"
    kind: Literal["gold"] = "gold"
    grams: Qty
    rate: Price  # per gram

    @field_validator("grams")
    @classmethod
    def _grams(cls, v: Qty) -> Qty:
        if v.micros > 10**6 * 10**7:  # ten tonnes
            raise ValueError("more than 10 tonnes of gold; check the number")
        return v

    @field_validator("rate")
    @classmethod
    def _rate(cls, v: Price) -> Price:
        if v.micro_rupees > 10**6 * 10**7:  # ₹1 crore a gram
            raise ValueError("a gold rate above ₹1 crore a gram; check the number")
        return v


class Closed(_Entry):
    """You no longer hold it (an FD paid out, a loan repaid, a property sold).
    History before `as_of` stays; from `as_of` on it counts as nothing."""

    entry: Literal["closed"] = "closed"
    kind: Kind


Entry = Annotated[Stated | Fd | Gold | Closed, Field(discriminator="entry")]
_ENTRY: TypeAdapter[Entry] = TypeAdapter(Entry)


# ----------------------------------------------------------------- valuing


def fd_maturity(fd: Fd) -> date:
    month = fd.start.month - 1 + fd.months
    year, month = fd.start.year + month // 12, month % 12 + 1
    day = fd.start.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1  # 31 Jan + 1 month -> 28/29 Feb


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year, month = d.year + month // 12, month % 12 + 1
    day = d.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1


def fd_value(fd: Fd, on: date) -> Money:
    """What the FD is worth on `on`, the way Indian banks accrue it:

    - under 6 months: simple interest, rate x days / 365;
    - otherwise compounded every full quarter (3 months from the start),
      with simple interest on the compounded amount for the days since the
      last quarter.

    Exact rational arithmetic, rounded half-even to the paise once. Before
    the start it is the principal; from maturity on it stops growing."""
    end = min(on, fd_maturity(fd))
    if end <= fd.start:
        return fd.principal
    rate = Fraction(fd.rate, 10_000)
    p = Fraction(fd.principal.paise)
    if fd.months < 6:
        value = p * (1 + rate * (end - fd.start).days / 365)
    else:
        months = (end.year - fd.start.year) * 12 + end.month - fd.start.month
        quarters = max(0, months // 3 + 1)
        while quarters > 0 and _add_months(fd.start, 3 * quarters) > end:
            quarters -= 1
        last = _add_months(fd.start, 3 * quarters)
        value = p * (1 + rate / 4) ** quarters * (1 + rate * (end - last).days / 365)
    return Money(paise=div_half_even(value.numerator, value.denominator))


def entry_value(e: Stated | Fd | Gold, on: date) -> Money:
    match e:
        case Stated():
            return e.amount
        case Fd():
            return fd_value(e, on)
        case Gold():
            return Money.of(e.grams, e.rate)


def current(entries: Iterable[Entry], today: date) -> tuple[Stated | Fd | Gold, ...]:
    """The latest snapshot per (kind, label key) dated on or before `today`,
    unless that snapshot closed it. On one day a close wins over a balance:
    "I hold it, and it ended today" means it ended. Order of `entries` never
    matters: the store keeps one value and one close per key and day."""
    latest: dict[tuple[str, str], Entry] = {}
    for e in entries:
        if e.as_of > today:
            continue
        key = (e.kind, label_key(e.label))
        held = latest.get(key)
        rank = (e.as_of, isinstance(e, Closed), _ENTRY.dump_json(e))
        if held is None or rank > (held.as_of, isinstance(held, Closed), _ENTRY.dump_json(held)):
            latest[key] = e
    out = [e for _, e in sorted(latest.items()) if not isinstance(e, Closed)]
    return tuple(out)


# ---------------------------------------------------------------- the view


class Line(Value):
    """One row of net worth: an entry you typed, or one class of market holdings."""

    kind: Kind | Literal["market"]
    label: str
    bucket: Bucket | None  # None: a loan
    value: Money
    as_of: date | None  # the day it was stated; None for market (see priced_on)
    days_old: int | None
    matured: date | None = None  # an FD past maturity, still counted at maturity value


class Slice(Value):
    bucket: Bucket
    name: str
    value: Money
    bps: Bps  # share of what you own; all slices add to exactly 10_000
    target: Bps | None
    drift: Bps | None  # bps - target, signed


class Note(Value):
    """One computed sentence, with the numbers it was built from."""

    tone: Literal["warn", "good", "info", "unknown"]
    kind: Literal["market_share", "drift", "on_target", "loans", "stale", "matured", "unpriced"]
    text: str
    bucket: Bucket | None = None
    bps: int | None = None


class NetWorth(Value):
    on: date
    owned: Money  # every asset, priced market holdings included
    owed: Money  # loans
    total: Money  # owned - owed
    market: Money  # priced market holdings only
    unpriced: tuple[str, ...]  # market holdings with no price, by name
    slices: tuple[Slice, ...]  # buckets with value, largest first
    lines: tuple[Line, ...]
    targets: dict[Bucket, Bps]
    notes: tuple[Note, ...]


def shares(values: list[int]) -> list[int]:
    """Largest-remainder split of 10_000 basis points: each share is within
    one basis point of exact, and they add to exactly 10_000. Ties go to the
    earlier value, so the answer depends only on the list."""
    total = sum(values)
    if total <= 0:
        raise ValueError("total must be positive")
    floors = [v * 10_000 // total for v in values]
    rems = [v * 10_000 - f * total for v, f in zip(values, floors, strict=True)]
    left = 10_000 - sum(floors)
    for i in sorted(range(len(values)), key=lambda i: (-rems[i], i))[:left]:
        floors[i] += 1
    return floors


_MARKET_BUCKET: Mapping[str, Bucket] = {
    "equity": "equity",
    "debt": "debt",
    "gold": "gold",
    "cash": "cash",
    "retirement": "retirement",
}


def networth(
    book: Book | None,
    prices: PriceBook,
    entries: Iterable[Entry],
    targets: Mapping[Bucket, Bps],
    today: date,
) -> NetWorth:
    """The whole picture on `today`. `book` is None before any CAS import."""
    lines: list[Line] = []
    unpriced: list[str] = []
    market_by: dict[Bucket, int] = {}
    if book is not None:
        ws, _, missing = weights(book, prices)
        for w in ws:
            inst = book.instrument(w.isin)
            bucket = _MARKET_BUCKET.get(inst.asset_class if inst else "equity", "equity")
            market_by[bucket] = market_by.get(bucket, 0) + w.value.paise
        for isin in missing:
            inst = book.instrument(isin)
            unpriced.append(inst.name if inst else isin)
    for bucket in BUCKETS:
        if bucket in market_by:
            lines.append(
                Line(
                    kind="market",
                    label=f"{BUCKET_NAME[bucket]} holdings",
                    bucket=bucket,
                    value=Money(paise=market_by[bucket]),
                    as_of=None,
                    days_old=None,
                )
            )
    for e in current(entries, today):
        matured = fd_maturity(e) if isinstance(e, Fd) and fd_maturity(e) <= today else None
        lines.append(
            Line(
                kind=e.kind,
                label=e.label,
                bucket=KIND_BUCKET[e.kind],
                value=entry_value(e, today),
                as_of=e.as_of,
                days_old=(today - e.as_of).days,
                matured=matured,
            )
        )

    owned = sum(ln.value.paise for ln in lines if ln.bucket is not None)
    owed = sum(ln.value.paise for ln in lines if ln.bucket is None)
    by_bucket: dict[Bucket, int] = {}
    for ln in lines:
        if ln.bucket is not None and ln.value.paise > 0:
            by_bucket[ln.bucket] = by_bucket.get(ln.bucket, 0) + ln.value.paise
    present: list[Bucket] = [b for b in BUCKETS if b in by_bucket]
    # every targeted bucket gets a slice, held or not: 0% of a 20% target is drift
    shown: list[Bucket] = [b for b in BUCKETS if b in by_bucket or b in targets]
    bps: dict[Bucket, int] = (
        dict(zip(present, shares([by_bucket[b] for b in present]), strict=True)) if present else {}
    )
    slices = [
        Slice(
            bucket=b,
            name=BUCKET_NAME[b],
            value=Money(paise=by_bucket.get(b, 0)),
            bps=Bps(bps.get(b, 0)),
            target=targets.get(b),
            drift=Bps(bps.get(b, 0) - targets[b]) if b in targets and present else None,
        )
        for b in shown
    ]
    slices.sort(key=lambda s: (-s.value.paise, BUCKETS.index(s.bucket)))
    market = sum(market_by.values())
    nw = NetWorth(
        on=today,
        owned=Money(paise=owned),
        owed=Money(paise=owed),
        total=Money(paise=owned - owed),
        market=Money(paise=market),
        unpriced=tuple(sorted(unpriced)),
        slices=tuple(slices),
        lines=tuple(lines),
        targets=dict(targets),
        notes=(),
    )
    return nw.model_copy(update={"notes": notes(nw)})


# ------------------------------------------------------------------- notes


def _p(bps: int) -> str:
    return pct(tenths_of(bps, 10_000))


def _points(tenths: int) -> str:
    """Tenths of a percentage point to text: 72 -> '7.2 points'."""
    whole, frac = divmod(abs(tenths), 10)
    return f"{whole}.{frac} point" + ("" if abs(tenths) == 10 else "s")


def notes(nw: NetWorth) -> tuple[Note, ...]:
    """The sentences under the chart, built from the same NetWorth object
    the chart draws. Looks-at-this first."""
    out: list[Note] = []
    owned = nw.owned.paise
    for s in nw.slices:
        if s.drift is None or s.target is None:
            continue
        # the gap is the shown weight minus the shown target, so the sentence's
        # own numbers always subtract to the points it states, and "outside the
        # band" is decided on those same shown numbers
        now, aim = tenths_of(s.bps, 10_000), tenths_of(s.target, 10_000)
        if abs(now - aim) > DRIFT_BAND // 10:
            side = "above" if s.drift > 0 else "below"
            out.append(
                Note(
                    tone="warn",
                    kind="drift",
                    bucket=s.bucket,
                    bps=s.drift,
                    text=(
                        f"{s.name} is {pct(now)} of what you own, {_points(now - aim)} "
                        f"{side} your target of {pct(aim)}."
                    ),
                )
            )
    drifted = [s for s in nw.slices if s.drift is not None]
    if drifted and not any(n.kind == "drift" for n in out):
        out.append(
            Note(
                tone="good",
                kind="on_target",
                text=f"Every class is within {_points(DRIFT_BAND // 10)} of your target mix.",
            )
        )
    for ln in nw.lines:
        if ln.matured is not None:
            out.append(
                Note(
                    tone="warn",
                    kind="matured",
                    text=(
                        f"{ln.label} matured on {ln.matured.isoformat()} at ₹{ln.value}; "
                        "add it to a bank balance or record the renewal."
                    ),
                )
            )
        elif ln.kind in ("bank", "epf", "ppf", "nps", "loan") and (ln.days_old or 0) > STALE_DAYS:
            out.append(
                Note(
                    tone="warn",
                    kind="stale",
                    text=(
                        f"{ln.label} was last updated {ln.days_old} days ago "
                        f"({ln.as_of.isoformat() if ln.as_of else ''}); update it from your "
                        "latest statement."
                    ),
                )
            )
    if owned > 0 and nw.owed.paise > 0:
        share = nw.owed.share_of(nw.owned)
        out.append(
            Note(
                tone="warn" if share > 5_000 else "info",
                kind="loans",
                bps=share,
                text=f"Loans are {_p(share)} of what you own: ₹{nw.owed} against ₹{nw.owned}.",
            )
        )
    if owned > 0 and nw.market.paise > 0:
        share = nw.market.share_of(nw.owned)
        out.append(
            Note(
                tone="info",
                kind="market_share",
                bps=share,
                text=(
                    f"Market holdings are {_p(share)} of what you own. Add, trim and exit "
                    "verdicts are measured against market holdings only."
                ),
            )
        )
    if nw.unpriced:
        n = len(nw.unpriced)
        out.append(
            Note(
                tone="unknown",
                kind="unpriced",
                text=(
                    f"{n} market holding{'s have' if n != 1 else ' has'} no price yet, "
                    f"so net worth leaves {'them' if n != 1 else 'it'} out."
                ),
            )
        )
    order = {"warn": 0, "unknown": 1, "good": 2, "info": 3}
    return tuple(sorted(out, key=lambda n: order[n.tone]))


# ------------------------------------------------------------------- store


def parse_targets(pairs: Iterable[str]) -> dict[Bucket, Bps]:
    """['equity=60', 'debt=25', 'gold=15'] -> basis points. They must add to
    exactly 100%. Raises ValueError with a sentence for the person."""
    from .thesis import parse_percent

    out: dict[Bucket, Bps] = {}
    for pair in pairs:
        name, sep, amount = pair.partition("=")
        bucket = name.strip().lower().replace(" ", "_").replace("-", "_")
        if not sep or bucket not in BUCKETS:
            raise ValueError(f"{pair!r} is not CLASS=PERCENT; classes are {', '.join(BUCKETS)}")
        if bucket in out:
            raise ValueError(f"{bucket} is given twice")
        out[_BUCKET.validate_python(bucket)] = parse_percent(amount)
    total = sum(out.values())
    if out and total != 10_000:
        raise ValueError(f"the targets add to {_p(total)}, not 100%")
    return {b: v for b, v in out.items() if v > 0}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  kind TEXT NOT NULL, key TEXT NOT NULL, as_of TEXT NOT NULL, closed INTEGER NOT NULL,
  body TEXT NOT NULL, PRIMARY KEY(kind, key, as_of, closed)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS targets(
  bucket TEXT PRIMARY KEY NOT NULL, bps INTEGER NOT NULL) WITHOUT ROWID;
"""


class AssetsDamaged(ValueError):
    """assets.db exists but cannot be read or written as Stockin's."""


_BPS: TypeAdapter[int] = TypeAdapter(Annotated[StrictInt, Field(gt=0, le=10_000)])
_BUCKET: TypeAdapter[Bucket] = TypeAdapter(Bucket)


class AssetStore:
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self._path = path

    @classmethod
    def open(cls, path: Path, *, readonly: bool = False) -> Self:
        """Creates assets.db (0600) on first write. `readonly` never creates
        or changes anything: `stockin doctor` uses it.

        Zero bytes is a new file. Any other file must pass SQLite's own
        integrity check and hold exactly Stockin's two tables, or it is
        damage (lesson 21): writing a schema into a file that lost its own
        would read as "nothing yet" and forget every entry for good."""
        if readonly and (not path.exists() or (path.is_file() and path.stat().st_size == 0)):
            raise FileNotFoundError(path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600)  # what you own is private
        if not path.is_file():
            raise AssetsDamaged(f"{path} is not a file")
        fresh = path.stat().st_size == 0
        try:
            conn = (
                sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
                if readonly
                else sqlite3.connect(path)
            )
        except sqlite3.Error as exc:
            raise AssetsDamaged(f"{path} could not be opened ({exc})") from None
        store = cls(conn, path)
        try:
            with _guard(path):
                if not fresh:
                    (ok,) = conn.execute("PRAGMA integrity_check(1)").fetchone()
                    if ok != "ok":
                        raise AssetsDamaged(f"{path} failed SQLite's integrity check ({ok})")
                    tables = {
                        n
                        for (n,) in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if tables != {"entries", "targets"}:
                        raise AssetsDamaged(
                            f"{path} does not hold Stockin's tables (found: "
                            f"{', '.join(sorted(tables)) or 'none'})"
                        )
                elif not readonly:
                    conn.executescript(_SCHEMA)
                store.entries()  # every row must read back
                store.targets()
        except AssetsDamaged:
            conn.close()
            raise
        return store

    def entries(self) -> tuple[Entry, ...]:
        with _guard(self._path):
            rows = self._conn.execute(
                "SELECT kind, key, as_of, closed, body FROM entries"
                " ORDER BY kind, key, as_of, closed"
            ).fetchall()
            out: list[Entry] = []
            for kind, key, as_of, closed, body in rows:
                e = _ENTRY.validate_json(body)
                mine = (e.kind, label_key(e.label), e.as_of.isoformat(), int(isinstance(e, Closed)))
                if mine != (kind, key, as_of, closed):
                    raise ValueError(f"row {kind}/{key}/{as_of} does not match its body")
                out.append(e)
        return tuple(out)

    def targets(self) -> dict[Bucket, Bps]:
        with _guard(self._path):
            rows = self._conn.execute("SELECT bucket, bps FROM targets ORDER BY bucket").fetchall()
            out: dict[Bucket, Bps] = {
                _BUCKET.validate_python(b): Bps(_BPS.validate_python(v)) for b, v in rows
            }
            if out and sum(out.values()) != 10_000:
                raise ValueError(f"the stored targets add to {sum(out.values())} bps, not 10000")
        return out

    def put(self, entry: Entry) -> None:
        """Add a snapshot. The same snapshot again changes nothing; another
        value for the same kind, label and day replaces it (a correction),
        and so does another close. A value and a close can share a day."""
        with _guard(self._path), self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?)",
                (
                    entry.kind,
                    label_key(entry.label),
                    entry.as_of.isoformat(),
                    int(isinstance(entry, Closed)),
                    _ENTRY.dump_json(entry).decode(),
                ),
            )

    def set_targets(self, targets: Mapping[Bucket, Bps]) -> None:
        """Replace the whole target mix; an empty mapping clears it."""
        if targets and sum(targets.values()) != 10_000:
            raise ValueError("targets must add to 100%")
        with _guard(self._path), self._conn:
            self._conn.execute("DELETE FROM targets")
            self._conn.executemany(
                "INSERT INTO targets VALUES (?,?)", [(b, int(v)) for b, v in targets.items()]
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@contextmanager
def _guard(path: Path) -> Generator[None]:
    try:
        yield
    except AssetsDamaged:
        raise
    except (sqlite3.DatabaseError, ValueError) as exc:
        raise AssetsDamaged(f"{path} could not be read or written ({exc})") from None
