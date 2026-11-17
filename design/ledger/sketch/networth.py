"""`stockin networth`: total, split by asset class, drift from target mix."""
from __future__ import annotations

from datetime import date, datetime
from typing import Mapping

from .book import Book
from .facts import AssetClass, FixedDeposit, GoldHolding, StatedBalance
from .money import Bps, Money, Value
from .prices import PriceBook


class Line(Value):
    """One row of net worth. `value is None` = unpriced, shown as such and
    excluded from the total and named in `NetWorth.gaps`."""

    label: str
    asset_class: AssetClass
    value: Money | None
    valued_at: datetime | date | None    # quote time, or the owner's as_of


class Slice(Value):
    asset_class: AssetClass
    value: Money
    weight: Bps
    target: Bps | None
    drift: Bps | None                    # weight - target


class NetWorth(Value):
    total: Money                         # of priced lines only
    gaps: tuple[str, ...]                # each unpriced line, named; () = complete
    slices: tuple[Slice, ...]            # weights sum to 10_000 (largest-remainder)
    lines: tuple[Line, ...]


def value_manual(asset: StatedBalance | FixedDeposit | GoldHolding,
                 prices: PriceBook, today: date) -> Line:
    """Exhaustive `match` on the union (pyright errors on a new variant):
      StatedBalance -> balance as stated; valued_at = as_of (no accrual guess)
      FixedDeposit  -> principal + quarterly-compounded accrual to
                       min(today, maturity), integer paise, half-even.
                       TODO: confirm compounding convention with owner.
      GoldHolding   -> Money.of(grams, prices[GOLD]) or None
    """
    raise NotImplementedError


def networth(book: Book, prices: PriceBook, today: date,
             targets: Mapping[AssetClass, Bps]) -> NetWorth:
    """Positions valued via Money.of(qty, quote.price) + manual lines.
    `targets` comes from config (must sum to 10_000; validated at load)."""
    raise NotImplementedError
