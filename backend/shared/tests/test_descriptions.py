from stash_shared import descriptions
from stash_shared.embeddings import MAX_INPUT_CHARS


def test_a_note_is_one_chunk_whatever_its_lines():
    note = "shopping\nmilk\neggs"

    # A note's text is its own description (and its "caption").
    assert descriptions.search_chunks(note, note) == [note]


def test_generated_chunks_are_one_per_line():
    generated = descriptions.from_chunks(["woman, girl, female", "cyberpunk city, futuristic urban environment"])

    assert descriptions.search_chunks(generated, None) == [
        "woman, girl, female",
        "cyberpunk city, futuristic urban environment",
    ]


def test_a_caption_is_one_chunk_before_the_generated_ones():
    caption = "my wallpaper\nfrom the game"
    description = descriptions.compose(caption, descriptions.from_chunks(["neon signs", "elevated highway"]))

    assert descriptions.search_chunks(description, caption) == [caption, "neon signs", "elevated highway"]


def test_caption_only():
    assert descriptions.search_chunks("our cat", "our cat") == ["our cat"]


def test_blank_and_repeated_chunks_are_dropped_and_long_ones_cut():
    long = "x" * (MAX_INPUT_CHARS + 10)

    assert descriptions.search_chunks(f"cat\n\n  \ncat\n{long}", None) == ["cat", "x" * MAX_INPUT_CHARS]


def test_nothing_to_search():
    assert descriptions.search_chunks(None, None) == []
    assert descriptions.search_chunks("", "") == []
