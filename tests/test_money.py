import pytest
from pydantic import ValidationError

from stockin.money import Money, Price, Qty, QtyDelta, div_half_even, percent


@pytest.mark.parametrize(
    ("n", "d", "expected"),
    [(15, 10, 2), (25, 10, 2), (35, 10, 4), (-15, 10, -2), (-25, 10, -2), (14, 10, 1), (16, 10, 2)],
)
def test_half_even(n: int, d: int, expected: int) -> None:
    assert div_half_even(n, d) == expected


def test_rupees_parses_indian_grouping() -> None:
    assert Money.rupees("8,45,000").paise == 84_500_000
    assert Money.rupees("1350.5").paise == 135_050
    assert Money.rupees("-12.34").paise == -1_234


@pytest.mark.parametrize("bad", ["1.234", "abc", "", "1e3", "--1"])
def test_rupees_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        Money.rupees(bad)


def test_float_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Money(paise=1.0)  # type: ignore[arg-type]


def test_qty_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        Qty(micros=-1)
    with pytest.raises(ValueError):
        Qty.parse("-1")


def test_value_of_nav_with_four_decimals() -> None:
    # 152.347 units at NAV 45.6789 = 6959.0433... -> 6959.04
    assert Money.of(Qty.parse("152.347"), Price.rupees("45.6789")).paise == 695_904
    assert Money.of(QtyDelta(micros=-1_000_000), Price.rupees("10")).paise == -1_000


def test_split_conserves_every_paisa() -> None:
    cost = Money(paise=100_001)
    share, rest = cost.split(Qty.parse("1"), Qty.parse("3"))
    assert share + rest == cost
    assert share.paise == 33_334


def test_share_and_percent() -> None:
    assert Money(paise=800).share_of(Money(paise=10_000)) == 800
    assert percent("8") == 800
    assert percent("7.1") == 710
    with pytest.raises(ValueError):
        percent("7.125")


def test_str() -> None:
    assert str(Money(paise=84_500_050)) == "845,000.50"
    assert str(Money(paise=-5)) == "-0.05"
