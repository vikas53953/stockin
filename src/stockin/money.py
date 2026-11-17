"""Exact numbers. No float appears in this module or in any type built on it.

Three integer scales, one rounding site:
  Money  paise                       (1e-2 INR)  amounts
  Qty    micro-units                 (1e-6 unit) MF units, shares, gold grams
  Price  micro-rupees per whole unit (1e-6 INR)  NAVs carry 4 decimals, so a
                                                 paise price would be lossy
`Money.of(qty, price)` is the only place a quantity meets a price, and it
rounds half-even to the paise.
"""

from __future__ import annotations

import re
from typing import NewType, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt

Bps = NewType("Bps", int)
"""Basis points, 1/100 of a percent. Weights, drift, FD rates, XIRR."""

_DECIMAL = re.compile(r"^(-)?([0-9]+)(?:\.([0-9]+))?$")
_GROUPED = re.compile(
    r"^-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,2}(?:,[0-9]{2})*,[0-9]{3})(?:\.[0-9]+)?$"
)
"""Western (1,234,567) or Indian (12,34,567) grouping, nothing else."""


def _parse_scaled(text: str, places: int, *, signed: bool) -> int:
    """'1,350.50' -> 135050 at places=2. ASCII digits only. Commas must form
    Western or Indian grouping. More than `places` decimals, a sign when
    unsigned, or anything that is not a plain decimal raises ValueError."""
    raw = text.strip()
    if "," in raw and not _GROUPED.match(raw):
        raise ValueError(f"misplaced comma: {text!r}")
    match = _DECIMAL.match(raw.replace(",", ""))
    if match is None:
        raise ValueError(f"not a decimal number: {text!r}")
    sign, whole, frac = match.group(1), match.group(2), match.group(3) or ""
    if sign and not signed:
        raise ValueError(f"must not be negative: {text!r}")
    if len(frac) > places:
        raise ValueError(f"more than {places} decimal places: {text!r}")
    value = int(whole) * 10**places + int(frac.ljust(places, "0"))
    return -value if sign else value


def div_half_even(numerator: int, denominator: int) -> int:
    """Integer division rounded half to even. The codebase's only rounding."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    quotient, remainder = divmod(numerator, denominator)
    twice = 2 * remainder
    if twice > denominator or (twice == denominator and quotient % 2 == 1):
        quotient += 1
    return quotient


def percent(text: str) -> Bps:
    """Boundary parser: "8" -> 800, "7.1" -> 710. At most 2 decimals."""
    return Bps(_parse_scaled(text, 2, signed=False))


class Value(BaseModel):
    """Base for every domain value: frozen, no extra fields, no coercion.
    Strict mode means a float passed where an int belongs fails validation."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Money(Value):
    """Signed amount in integer paise."""

    paise: StrictInt

    @classmethod
    def rupees(cls, text: str) -> Self:
        """Boundary parser for CLI and CAS decimal strings ("845000",
        "8,45,000.50"). Raises ValueError on more than 2 decimals."""
        return cls(paise=_parse_scaled(text, 2, signed=True))

    @classmethod
    def of(cls, qty: Qty | QtyDelta, price: Price) -> Self:
        """Value of `qty` units at `price`, rounded half-even to the paise."""
        return cls(paise=div_half_even(qty.micros * price.micro_rupees, 10**10))

    def __add__(self, other: Money) -> Money:
        return Money(paise=self.paise + other.paise)

    def __sub__(self, other: Money) -> Money:
        return Money(paise=self.paise - other.paise)

    def __neg__(self) -> Money:
        return Money(paise=-self.paise)

    def split(self, part: Qty, whole: Qty) -> tuple[Money, Money]:
        """(share for `part` of `whole`, remainder). The two always add back
        to self exactly, so a partial sell never leaks a paise of cost."""
        if whole.micros == 0 or part.micros > whole.micros:
            raise ValueError("part must be within a non-empty whole")
        share = Money(paise=div_half_even(self.paise * part.micros, whole.micros))
        return share, self - share

    def share_of(self, total: Money) -> Bps:
        """self / total in basis points, half-even. total must be positive."""
        if total.paise <= 0:
            raise ValueError("total must be positive")
        return Bps(div_half_even(self.paise * 10_000, total.paise))

    def __str__(self) -> str:
        sign = "-" if self.paise < 0 else ""
        rupees, paise = divmod(abs(self.paise), 100)
        return f"{sign}{_indian_grouping(rupees)}.{paise:02d}"


def _indian_grouping(n: int) -> str:
    """845000 -> '8,45,000'. Last three digits, then pairs."""
    digits = str(n)
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs = [head[max(0, i - 2) : i] for i in range(len(head), 0, -2)]
    return ",".join(reversed(pairs)) + "," + tail


class Qty(Value):
    """A held quantity in micro-units. Negative holdings are unrepresentable."""

    micros: StrictInt = Field(ge=0)

    @classmethod
    def parse(cls, text: str) -> Self:
        """Boundary parser ("152.347"). Raises on >6 decimals or a sign."""
        return cls(micros=_parse_scaled(text, 6, signed=False))

    def __str__(self) -> str:
        """Exact, trailing zeros dropped: 152.347, 10, 0.000001."""
        whole, frac = divmod(self.micros, 1_000_000)
        return f"{whole}.{frac:06d}".rstrip("0").rstrip(".")


class QtyDelta(Value):
    """Signed change in quantity, in micro-units."""

    micros: StrictInt


class Price(Value):
    """Price of one whole unit, in micro-rupees. Always positive."""

    micro_rupees: StrictInt = Field(gt=0)

    @classmethod
    def rupees(cls, text: str) -> Self:
        """Boundary parser ("1350", "45.6789"). Raises on >6 decimals."""
        return cls(micro_rupees=_parse_scaled(text, 6, signed=False))
