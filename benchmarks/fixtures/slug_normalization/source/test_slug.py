from slug import slugify


def test_slugify_two_words() -> None:
    assert slugify("Hello World") == "hello-world"

