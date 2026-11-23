"""Your thesis for each holding, and the verdict it gives: add, trim, exit,
hold, or review.

A verdict is never advice from Stockin. It is your own rule applied to your
own numbers: the reason you wrote for holding something, the weight you
said it should have, and whether you have marked the reason as broken. Code
works out the verdict and the sentence that explains it from the same
numbers, the way insights do.

theses.db sits beside ledger.db and is private (0600) like it. It is the
only file here that you write by hand, so it is kept apart from statement
facts: rebuilding the ledger never touches it.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Self

from pydantic import Field

from .book import Book
from .facts import Isin
from .insights import (
    CONCENTRATED_ONE,
    Weight,
    pct,
    short_name,
    tenths_of,
    weights,
)
from .money import Bps, Price, Value
from .prices import BAND_DAYS, PriceBook, rupee_text

Call = Literal["add", "trim", "exit", "hold", "review"]

BAND = Bps(500)
"""How far from your target weight a holding may drift before the verdict
says add or trim: 5 percentage points either way."""

MAX_TEXT = 2_000


def parse_percent(text: str) -> Bps:
    """'12', '12.5' or '12.5%' to basis points, exactly (no float); 0 to 100.
    Raises ValueError with a sentence for the person."""
    m = re.fullmatch(r"(\d{1,3})(?:\.(\d{1,2}))?\s*%?", text.strip())
    if m is None:
        raise ValueError(f"{text.strip()!r} is not a percent like 10 or 12.5")
    bps = int(m[1]) * 100 + int((m[2] or "0").ljust(2, "0"))
    if bps > 10_000:
        raise ValueError(f"{text.strip()!r} is above 100%")
    return Bps(bps)


class Thesis(Value):
    isin: Isin
    why: str = Field(min_length=1, max_length=MAX_TEXT)
    exit_if: str = Field(default="", max_length=MAX_TEXT)
    target: Bps | None = None  # your target weight, of priced value
    broken: bool = False  # you marked the reason as no longer true
    written: date


class Verdict(Value):
    isin: Isin
    name: str
    call: Call
    because: str
    weight: Bps | None
    target: Bps | None
    weight_text: str = ""  # the weight exactly as `because` prints it
    target_text: str = ""  # the target exactly as `because` prints it
    unsure: bool = False  # review because the price is in doubt (`doubts`)


def _hundredths(bps: int) -> str:
    return f"{bps // 100}.{bps % 100:02d}%"


def _shown(w: int, other: int, tenths_ok: bool) -> tuple[str, str]:
    """Weight and target as the sentence prints them: tenths of a percent,
    unless rounding to tenths would make the sentence contradict its own
    call (17.46% shown as 17.5% "more than 5 points above" 12.5%); then
    hundredths, which are exact for basis points."""
    if tenths_ok:
        return pct(tenths_of(w, 10_000)), pct(tenths_of(other, 10_000))
    return _hundredths(w), _hundredths(other)


def verdict(
    isin: Isin,
    name: str,
    weight: Weight | None,
    thesis: Thesis | None,
    money: str = "your money",
    doubt: str | None = None,
) -> Verdict:
    """Your rule, applied in this order: a broken thesis says exit; no
    thesis or no price says review, and so does a price that is not sure
    (`doubt` is the sentence that says why, see `doubts`); with a target, more than BAND above it
    says trim and more than BAND below says add; with no target, above the
    25% line says trim; otherwise hold."""
    w = weight.bps if weight else None
    target = thesis.target if thesis else None

    def out(
        call: Call, because: str, now: str = "", aim: str = "", unsure: bool = False
    ) -> Verdict:
        return Verdict(
            isin=isin,
            name=name,
            call=call,
            because=because,
            weight=w,
            target=target,
            weight_text=now,
            target_text=aim,
            unsure=unsure,
        )

    def p(bps: int) -> str:
        return pct(tenths_of(bps, 10_000))

    if thesis is not None and thesis.broken:
        stop = "" if thesis.exit_if.endswith((".", "!", "?")) else "."
        why = f" You said you would exit if: {thesis.exit_if}{stop}" if thesis.exit_if else ""
        return out("exit", f"You marked your reason for holding {name} as broken.{why}")
    if thesis is None:
        return out("review", f"No thesis yet: write why you hold {name} to get a verdict.")
    if w is None:
        return out("review", f"{name} has no price yet, so its weight is unknown.")
    if doubt is not None:
        return out("review", doubt, unsure=True)
    if target is not None:
        band = p(BAND)[:-1] + " points"
        gap = tenths_of(w, 10_000) - tenths_of(target, 10_000)  # as the sentence shows it
        if w > target + BAND:
            now, aim = _shown(w, target, gap > BAND // 10)
            return out(
                "trim",
                f"{name} is {now} of {money}, more than {band} above your target of {aim}.",
                now,
                aim,
            )
        if w < target - BAND:
            now, aim = _shown(w, target, -gap > BAND // 10)
            return out(
                "add",
                f"{name} is {now} of {money}, more than {band} below your target of {aim}.",
                now,
                aim,
            )
        now, aim = _shown(w, target, abs(gap) <= BAND // 10)
        return out(
            "hold", f"{name} is {now} of {money}, within {band} of your target of {aim}.", now, aim
        )
    if w > CONCENTRATED_ONE:
        line = p(CONCENTRATED_ONE)
        (now, _) = _shown(w, CONCENTRATED_ONE, tenths_of(w, 10_000) > CONCENTRATED_ONE // 10)
        return out(
            "trim",
            f"{name} is {now} of {money}, above the {line} line, and you set no target.",
            now,
        )
    return out("hold", f"Your thesis stands and {name} is {p(w)} of {money}.", p(w))


def verdicts(
    names: dict[Isin, str],
    weights: tuple[Weight, ...],
    theses: dict[Isin, Thesis],
    money: str = "your money",
    doubt: Mapping[Isin, str] | None = None,
) -> tuple[Verdict, ...]:
    """One verdict per held ISIN, the ones that ask for action first."""
    by = {w.isin: w for w in weights}
    order = {"exit": 0, "trim": 1, "add": 2, "review": 3, "hold": 4}
    why = doubt or {}
    found = [verdict(i, n, by.get(i), theses.get(i), money, why.get(i)) for i, n in names.items()]
    found.sort(key=lambda v: (order[v.call], -(v.weight or 0), v.isin))
    return tuple(found)


MAX_PRICE_AGE = timedelta(days=BAND_DAYS)
"""How old a price may be and still give add, trim or hold: the same week
as the price band (prices.BAND_DAYS)."""


def doubts(names: Mapping[Isin, str], prices: PriceBook, today: date) -> dict[Isin, str]:
    """Why a holding's price is not one to act on, as the sentence its
    verdict says; a holding with a sure price is not in the result.

    A verdict can reach the phone with no one looking, so it is worked out
    only from prices Stockin is sure of (#39 verifier round 2, R-1):

    - a newer price that a file gave was held back (PriceBook.doubted). The
      price in use may then be wrong by any amount, and every weight is a
      share of the same total, so no holding gets add, trim or hold until
      the owner has settled it;
    - the price is more than MAX_PRICE_AGE old. Nothing says it is wrong,
      so only its own holding waits.

    Each sentence is built from the quotes themselves: the price, its day,
    and the command that settles it."""
    held = {i: h for i in sorted(names) if (h := prices.doubted(i)) is not None}
    out: dict[Isin, str] = {}
    for isin, name in names.items():
        used = prices.get(isin)
        if used is None:
            continue  # "no price yet" says it already
        if isin in held:
            new = held[isin]
            out[isin] = (
                f"{name}'s newest price, {rupee_text(new.price)} on {new.on}, was not stored: "
                f"it is too far from {rupee_text(used.price)} on {used.on}, the price in use. "
                f"No add or trim is worked out until that is settled. {_accept(new.price)}"
            )
        elif held:
            other, new = next(iter(held.items()))
            out[isin] = (
                f"{name}'s weight is not sure: the newest price of {names[other]}, "
                f"{rupee_text(new.price)} on {new.on}, was not stored, and every weight is a "
                f"share of the same total. {_accept(new.price)}"
            )
        elif today - used.on > MAX_PRICE_AGE:
            out[isin] = (
                f"{name}'s price, {rupee_text(used.price)}, is from {used.on}, more than "
                f"{BAND_DAYS} days ago, so no add or trim is worked out from it. "
                "Run `stockin prices fetch`."
            )
    return out


def _accept(price: Price) -> str:
    """What settles it. `prices fetch --accept-jumps` stores every price held
    in today's files, not only this one (#39 verify-3 C-6), so it says so and
    names the narrower way: importing the one file with the flag."""
    return (
        f"If {rupee_text(price)} is right, import its file with "
        "`stockin prices import FILE --accept-jumps`, or run "
        "`stockin prices fetch --accept-jumps`, which stores every price held "
        "in today's files."
    )


def for_book(
    book: Book, prices: PriceBook, theses: dict[Isin, Thesis], today: date
) -> tuple[Verdict, ...]:
    """A verdict for every ISIN you hold, from the same weights the map
    uses. `today` is India's day: a price is too old, or not, against it."""
    ws, _, unpriced = weights(book, prices)
    names: dict[Isin, str] = {}
    for pos in book.positions():
        inst = book.instrument(pos.isin)
        names[pos.isin] = short_name(inst.name) if inst else pos.isin
    # weights are shares of what has a price; say so while anything has none
    money = "your priced money" if unpriced and ws else "your money"
    return verdicts(names, ws, theses, money, doubts(names, prices, today))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS theses(
  isin TEXT PRIMARY KEY NOT NULL, body TEXT NOT NULL) WITHOUT ROWID
"""


class ThesesDamaged(ValueError):
    """theses.db exists but cannot be read as Stockin's."""


class ThesisStore:
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self._path = path

    @classmethod
    def open(cls, path: Path) -> Self:
        new = not path.exists()
        if new:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600)  # what you think of your holdings is private
        if not path.is_file():
            raise ThesesDamaged(f"{path} is not a file")
        conn = sqlite3.connect(path)
        try:
            with _guard(path):
                conn.execute(_SCHEMA)
                cls(conn, path).all()  # every row must read back
        except ThesesDamaged:
            conn.close()
            raise
        return cls(conn, path)

    def all(self) -> dict[Isin, Thesis]:
        with _guard(self._path):
            rows = self._conn.execute("SELECT body FROM theses").fetchall()
            found = [Thesis.model_validate_json(b) for (b,) in rows]
        return {t.isin: t for t in found}

    def put(self, thesis: Thesis) -> None:
        with _guard(self._path), self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO theses VALUES (?, ?)",
                (thesis.isin, thesis.model_dump_json()),
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
    except (sqlite3.DatabaseError, ValueError) as exc:
        raise ThesesDamaged(f"{path} could not be read or written ({exc})") from None
