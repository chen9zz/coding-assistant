from calculator import safe_divide


def test_safe_divide_regular_values() -> None:
    assert safe_divide(6, 2) == 3

