from decimal import Decimal

from app import Calculator


def test_divide_regular_values() -> None:
    assert Calculator().divide(Decimal("6"), Decimal("2")) == Decimal("3")

