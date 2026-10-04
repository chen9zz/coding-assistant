from slug import slugify


def test_slugify_collapses_punctuation_and_whitespace() -> None:
    assert slugify("  Agent,   Safety!  ") == "agent-safety"

