"""Exact numbers. No float appears in this module or in any type built on it.

Three integer scales, one rounding site:
  Money  paise                       (1e-2 INR)  amounts
  Qty    micro-units                 (1e-6 unit) MF units, shares, gold grams
  Price  micro-rupees per whole unit (1e-6 INR)  NAVs carry 4 decimals, so a
                                                 paise price would be lossy
`Money.of(qty, price)` is the ONLY place a quantity meets a price and the
only place rounding happens (half-even to the paise).
"""
from __future__ import annotations

from typing import NewType, Self

from pydantic import BaseModel, ConfigDict, StrictInt, Field

Bps = NewType("Bps", int)
"""Basis points, 1/100 of a percent. Weights, drift, FD rates, XIRR."""


def percent(text: str) -> Bps:
    """Boundary parser: "8" -> 800, "7.1" -> 710. Decimal inside, max 2
    decimals, raises ValueError otherwise. Rates and weights enter here."""
    raise NotImplementedError


class Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Money(Value):
    """Signed amount in integer paise. StrictInt: a float fails validation."""

    paise: StrictInt

    @classmethod
    def rupees(cls, text: str) -> Self:
        """Boundary parser for CLI/CAS decimal strings ("845000", "1,350.50").
        Raises ValueError on more than 2 decimals. Never accepts a float."""
        raise NotImplementedError

    @classmethod
    def of(cls, qty: Qty | QtyDelta, price: Price) -> Self:
        """round_half_even(qty.micros * price.micro_rupees / 10**10) paise."""
        raise NotImplementedError

    def __add__(self, other: Money) -> Money: raise NotImplementedError
    def __sub__(self, other: Money) -> Money: raise NotImplementedError
    def __neg__(self) -> Money: raise NotImplementedError

    def split(self, part: Qty, whole: Qty) -> tuple[Money, Money]:
        """(share for `part`, remainder), share + remainder == self exactly.
        Used when a sell consumes part of a lot: cost never leaks a paise."""
        raise NotImplementedError

    def share_of(self, total: Money) -> Bps:
        """self / total in bps, half-even. total must be > 0."""
        raise NotImplementedError


class Qty(Value):
    """A held quantity. Invariant: micros >= 0. Negative holdings are
    unrepresentable; a fold that would go negative is a reconcile Mismatch."""

    micros: StrictInt = Field(ge=0)

    @classmethod
    def parse(cls, text: str) -> Self:
        """Boundary parser ("152.347"). Raises on >6 decimals or a sign."""
        raise NotImplementedError


class QtyDelta(Value):
    """Signed change in quantity. Only Timeline movements and gaps use it."""

    micros: StrictInt


class Price(Value):
    """Price of ONE whole unit, in micro-rupees. Invariant: > 0."""

    micro_rupees: StrictInt = Field(gt=0)

    @classmethod
    def rupees(cls, text: str) -> Self:
        raise NotImplementedError
