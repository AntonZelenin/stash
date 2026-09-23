import pytest

from app.items.services import build_prefix_tsquery


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("cat", "cat:*"),
        ("Orange  Oval", "orange:* & oval:*"),
        ("  screenshot of code ", "screenshot:* & of:* & code:*"),
        ("github.com", "github:* & com:*"),
        ("кот", "кот:*"),
        ("v2", "v2:*"),
    ],
)
def test_builds_prefix_and_query(query, expected):
    assert build_prefix_tsquery(query) == expected


@pytest.mark.parametrize("query", ["", "   ", "&|!():*'\"", "-"])
def test_returns_none_when_nothing_searchable(query):
    assert build_prefix_tsquery(query) is None


def test_tsquery_syntax_in_input_is_stripped():
    # Would be a to_tsquery syntax error if passed through.
    assert build_prefix_tsquery("cat & (dog | !bird):*") == "cat:* & dog:* & bird:*"


def test_caps_number_of_terms():
    assert build_prefix_tsquery(" ".join(f"w{i}" for i in range(50))).count(":*") == 10
