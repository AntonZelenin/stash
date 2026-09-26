"""How an image's or file's searchable text (`item_descriptions`) is built
from its user caption and its generated description, shared by the content
analyzers (which write it once analysis is done) and the API (which rebuilds
it when the caption is edited), so both agree on the format. Also how the
embedding worker splits that text into the chunks it embeds
(`search_chunks`)."""

from stash_shared.embeddings import MAX_INPUT_CHARS

_SEPARATOR = "\n\n"
# A generated image description is a list of short search chunks (see the
# image analyzer), stored one per line.
_CHUNK_SEPARATOR = "\n"


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


def from_chunks(chunks: list[str]) -> str:
    """A generated description made of search chunks, one per line."""
    return _CHUNK_SEPARATOR.join(chunks)


def search_chunks(description: str | None, caption: str | None) -> list[str]:
    """What semantic search embeds for an item, one vector per chunk: the
    user's own text (a note's or link's text, a caption) as one chunk, then
    each line of the generated description as a chunk of its own (an
    image's search chunks; a document's description is normally a single
    one). Empty chunks and repeats are dropped, and each is cut to what can
    be embedded. `description` was composed with `caption` (see `compose`)."""
    if not description:
        return []
    generated = generated_part(description, caption)
    if generated is None:
        parts = [description]
    else:
        parts = [caption] if caption and description.startswith(caption + _SEPARATOR) else []
        parts.extend(generated.splitlines())
    chunks: list[str] = []
    for part in parts:
        chunk = part.strip()[:MAX_INPUT_CHARS]
        if chunk and chunk not in chunks:
            chunks.append(chunk)
    return chunks
