import pytest

from config import parse_port


def test_parse_port_uses_default_for_missing_value() -> None:
    assert parse_port(None) == 8080


def test_parse_port_rejects_out_of_range_value() -> None:
    with pytest.raises(ValueError):
        parse_port("70000")

