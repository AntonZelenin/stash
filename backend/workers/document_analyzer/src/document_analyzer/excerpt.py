"""Choosing what part of a document's text goes to the model.

The model only needs enough to tell what a document *is* and what it's
about, not all of it, so the input is capped (`max_chars`, configured
separately from the upload size limit). Text within the cap is sent whole.
Longer text is represented by its beginning — where titles, abstracts,
tables of contents and introductions are, usually the most telling part —
plus evenly spaced samples from the rest, so later topics are represented
too. Parts are cut at whitespace and joined by a visible marker, and the
model is told it's looking at excerpts.

Characters rather than tokens: no tokenizer dependency, and for a budget
like this the ~4 characters/token rule of thumb is precise enough.

Large plain-text files get the same excerpts without being read whole
(`excerpt_from_ranges`): just the byte ranges the excerpts come from.
"""

from collections.abc import Callable
from dataclasses import dataclass

SEPARATOR = "\n\n[…]\n\n"
# Share of the budget given to the beginning; the rest is split evenly
# between the samples.
_HEAD_SHARE = 0.5
_SAMPLES = 4
# UTF-8's widest character: reading this many bytes per character wanted
# always yields at least that many characters.
_MAX_BYTES_PER_CHAR = 4


@dataclass(frozen=True)
class Excerpt:
    text: str
    # True if `text` is excerpts of a longer document rather than all of it.
    is_partial: bool


def _budgets(max_chars: int) -> tuple[int, int]:
    """Characters for the beginning, and for each sample."""
    budget = max_chars - _SAMPLES * len(SEPARATOR)
    head_chars = int(budget * _HEAD_SHARE)
    return head_chars, (budget - head_chars) // _SAMPLES


def select_excerpt(text: str, max_chars: int) -> Excerpt:
    if len(text) <= max_chars:
        return Excerpt(text=text, is_partial=False)

    head_chars, sample_chars = _budgets(max_chars)

    parts = [_cut(text, 0, head_chars)]
    # Split what follows the head into equal slices and take a window from
    # the middle of each.
    rest_start = head_chars
    slice_chars = (len(text) - rest_start) // _SAMPLES
    for index in range(_SAMPLES):
        slice_start = rest_start + index * slice_chars
        window_start = slice_start + max(0, (slice_chars - sample_chars) // 2)
        parts.append(_cut(text, window_start, sample_chars))

    return Excerpt(text=SEPARATOR.join(part for part in parts if part), is_partial=True)


def excerpt_from_ranges(
    size: int, read: Callable[[int, int], bytes], decode: Callable[[bytes], str | None], max_chars: int
) -> str:
    """The excerpts `select_excerpt` would pick from a plain-text file of
    `size` bytes, read in ranges instead of whole: its beginning, then a
    sample from the middle of each of `_SAMPLES` equal slices of the rest,
    at most `max_chars` characters in all. Positions are in bytes, so
    they're only close to where `select_excerpt` would take them.

    `read(start, end)` reads bytes. `decode` decodes a range of them
    (dropping characters cut at its edges), or returns None if the text
    can't be decoded from an arbitrary position (e.g. UTF-16): then there
    are no samples. Reads at most 4 bytes per character kept."""
    head_chars, sample_chars = _budgets(max_chars)
    head_end = min(size, head_chars * _MAX_BYTES_PER_CHAR)
    head = decode(read(0, head_end))
    if head is None:
        return ""
    parts = [_words(head[:head_chars], starts_mid_text=False, ends_mid_text=head_end < size)]

    slice_bytes = (size - head_end) // _SAMPLES
    sample_bytes = sample_chars * _MAX_BYTES_PER_CHAR
    for index in range(_SAMPLES):
        start = head_end + index * slice_bytes + max(0, (slice_bytes - sample_bytes) // 2)
        end = min(size, start + sample_bytes)
        if start >= end:
            continue
        sample = decode(read(start, end))
        if sample is None:
            break
        parts.append(_words(sample[:sample_chars], starts_mid_text=True, ends_mid_text=end < size))
    return SEPARATOR.join(part for part in parts if part)


def _cut(text: str, start: int, length: int) -> str:
    """`text[start:start + length]`, shrunk to whole words (see `_words`).
    Never longer than `length`."""
    return _words(
        text[start : start + length], starts_mid_text=start > 0, ends_mid_text=start + length < len(text)
    )


def _words(window: str, *, starts_mid_text: bool, ends_mid_text: bool) -> str:
    """`window`, a stretch of a longer text, shrunk to whole words: it
    starts after its first whitespace if it starts mid-text, and ends at its
    last one if it ends mid-text, so no word is cut in half."""
    if starts_mid_text:
        first_space = next((i for i, char in enumerate(window) if char.isspace()), None)
        window = window[first_space + 1 :] if first_space is not None else window
    if ends_mid_text:
        last_space = max(window.rfind(" "), window.rfind("\n"))
        window = window[:last_space] if last_space > 0 else window
    return window.strip()
