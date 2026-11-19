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
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
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
from .money import Bps, Value
from .prices import PriceBook

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


def verdict(
    isin: Isin,
    name: str,
    weight: Weight | None,
    thesis: Thesis | None,
    money: str = "your money",
) -> Verdict:
    """Your rule, applied in this order: a broken thesis says exit; no
    thesis or no price says review; with a target, more than BAND above it
    says trim and more than BAND below says add; with no target, above the
    25% line says trim; otherwise hold."""
    w = weight.bps if weight else None
    target = thesis.target if thesis else None

    def out(call: Call, because: str) -> Verdict:
        return Verdict(isin=isin, name=name, call=call, because=because, weight=w, target=target)

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
    if target is not None:
        now, aim, band = p(w), p(target), p(BAND)[:-1] + " points"
        if w > target + BAND:
            return out(
                "trim",
                f"{name} is {now} of {money}, more than {band} above your target of {aim}.",
            )
        if w < target - BAND:
            return out(
                "add",
                f"{name} is {now} of {money}, more than {band} below your target of {aim}.",
            )
        return out("hold", f"{name} is {now} of {money}, within {band} of your target of {aim}.")
    if w > CONCENTRATED_ONE:
        line = p(CONCENTRATED_ONE)
        return out(
            "trim", f"{name} is {p(w)} of {money}, above the {line} line, and you set no target."
        )
    return out("hold", f"Your thesis stands and {name} is {p(w)} of {money}.")


def verdicts(
    names: dict[Isin, str],
    weights: tuple[Weight, ...],
    theses: dict[Isin, Thesis],
    money: str = "your money",
) -> tuple[Verdict, ...]:
    """One verdict per held ISIN, the ones that ask for action first."""
    by = {w.isin: w for w in weights}
    order = {"exit": 0, "trim": 1, "add": 2, "review": 3, "hold": 4}
    found = [verdict(i, n, by.get(i), theses.get(i), money) for i, n in names.items()]
    found.sort(key=lambda v: (order[v.call], -(v.weight or 0), v.isin))
    return tuple(found)


def for_book(book: Book, prices: PriceBook, theses: dict[Isin, Thesis]) -> tuple[Verdict, ...]:
    """A verdict for every ISIN you hold, from the same weights the map uses."""
    ws, _, unpriced = weights(book, prices)
    names: dict[Isin, str] = {}
    for pos in book.positions():
        inst = book.instrument(pos.isin)
        names[pos.isin] = short_name(inst.name) if inst else pos.isin
    # weights are shares of what has a price; say so while anything has none
    money = "your priced money" if unpriced and ws else "your money"
    return verdicts(names, ws, theses, money)


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
        raise ThesesDamaged(f"{path} could not be read ({exc})") from None
