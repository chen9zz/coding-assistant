from config import parse_port


def test_parse_port_valid_value() -> None:
    assert parse_port("3000") == 3000

