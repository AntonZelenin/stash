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
"""

from dataclasses import dataclass

SEPARATOR = "\n\n[…]\n\n"
# Share of the budget given to the beginning; the rest is split evenly
# between the samples.
_HEAD_SHARE = 0.5
_SAMPLES = 4


@dataclass(frozen=True)
class Excerpt:
    text: str
    # True if `text` is excerpts of a longer document rather than all of it.
    is_partial: bool


def select_excerpt(text: str, max_chars: int) -> Excerpt:
    if len(text) <= max_chars:
        return Excerpt(text=text, is_partial=False)

    budget = max_chars - _SAMPLES * len(SEPARATOR)
    head_chars = int(budget * _HEAD_SHARE)
    sample_chars = (budget - head_chars) // _SAMPLES

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


def _cut(text: str, start: int, length: int) -> str:
    """`text[start:start + length]`, shrunk to whole words: it starts after
    the first whitespace (unless at the very beginning) and ends at the last
    one, so no word is cut in half. Never longer than `length`."""
    window = text[start : start + length]
    if start > 0:
        first_space = next((i for i, char in enumerate(window) if char.isspace()), None)
        window = window[first_space + 1 :] if first_space is not None else window
    if start + length < len(text):
        last_space = max(window.rfind(" "), window.rfind("\n"))
        window = window[:last_space] if last_space > 0 else window
    return window.strip()
