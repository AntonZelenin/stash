import pytest

from app.items.services import TextContentKind, text_content_kind


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("https://example.com", TextContentKind.url_only),
        ("HTTP://Example.com/a?b=1#c", TextContentKind.url_only),
        ("just a note", TextContentKind.no_url),
        ("example.com", TextContentKind.no_url),
        ("ftp://example.com", TextContentKind.no_url),
        ("http://", TextContentKind.no_url),
        ("see https://example.com", TextContentKind.mixed),
        ("(https://example.com).", TextContentKind.mixed),
        ("https://example.com https://example.org", TextContentKind.mixed),
        ("https://example.com\nnotes", TextContentKind.mixed),
    ],
)
def test_text_content_kind(text: str, kind: TextContentKind):
    assert text_content_kind(text) == kind
