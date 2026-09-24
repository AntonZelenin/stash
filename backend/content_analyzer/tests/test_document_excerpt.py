from content_analyzer.documents.excerpt import SEPARATOR, select_excerpt


def _words(count: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{i}" for i in range(count))


def test_text_within_limit_is_sent_whole():
    text = _words(100)

    excerpt = select_excerpt(text, max_chars=len(text))

    assert excerpt.text == text
    assert not excerpt.is_partial


def test_long_text_is_cut_to_the_limit():
    excerpt = select_excerpt(_words(50_000), max_chars=5_000)

    assert excerpt.is_partial
    assert len(excerpt.text) <= 5_000
    # Most of the budget is actually used, not left empty.
    assert len(excerpt.text) > 4_500


def test_keeps_the_beginning_and_samples_the_rest_in_order():
    sections = [_words(2_000, prefix=f"s{n}x") for n in range(8)]
    text = "\n".join(sections)

    excerpt = select_excerpt(text, max_chars=4_000)

    parts = excerpt.text.split(SEPARATOR)
    assert len(parts) == 5
    assert parts[0].startswith("s0x0 ")  # the very beginning
    # Samples come from across the document, front to back.
    first_words = [part.split()[0] for part in parts]
    section_of = [int(word[1 : word.index("x")]) for word in first_words]
    assert section_of == sorted(section_of)
    assert section_of[-1] >= 6


def test_parts_never_split_words():
    text = _words(50_000)

    excerpt = select_excerpt(text, max_chars=3_000)

    words = set(text.split())
    for part in excerpt.text.split(SEPARATOR):
        assert all(word in words for word in part.split()), part[:40]
