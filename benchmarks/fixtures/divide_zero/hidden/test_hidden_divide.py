from calculator import safe_divide


def test_safe_divide_zero_denominator_returns_none() -> None:
    assert safe_divide(6, 0) is None

