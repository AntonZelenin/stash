"""How an image's or file's searchable text (`item_descriptions`) is built
from its user caption and its generated description, shared by the content
analyzers (which write it once analysis is done) and the API (which rebuilds
it when the caption is edited), so both agree on the format."""

_SEPARATOR = "\n\n"


def compose(caption: str | None, generated: str | None) -> str | None:
    """Caption first, then the generated description; either may be
    missing. None if there's neither."""
    parts = [part for part in (caption, generated) if part]
    return _SEPARATOR.join(parts) if parts else None


def generated_part(description: str | None, caption: str | None) -> str | None:
    """The generated description inside `description`, which was composed
    with `caption` (see `compose`). None if there's none yet — e.g. the
    item is still being analyzed, or its analysis failed."""
    if not description:
        return None
    if not caption:
        return description
    if description == caption:
        return None
    prefix = caption + _SEPARATOR
    if description.startswith(prefix):
        return description[len(prefix) :] or None
    # Not composed from this caption (shouldn't happen): keep all of it
    # rather than lose the generated text.
    return description
