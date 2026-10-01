from stash_worker_core.chunks import clean_chunks


def test_one_line_each_without_blanks_repeats_or_non_strings():
    seen: set[str] = set()

    assert clean_chunks(["  dog\n running ", "", " ", "DOG RUNNING", 3, None, "beach"], seen) == [
        "dog running",
        "beach",
    ]
    assert seen == {"dog running", "beach"}


def test_repeats_of_earlier_lists_are_left_out():
    seen: set[str] = set()
    clean_chunks(["beach"], seen)

    assert clean_chunks(["Beach", "sea"], seen) == ["sea"]
