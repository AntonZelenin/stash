"""Tag-name rules, shared by tagging existing items (`app.tags.services`)
and tagging items as they're created (`app.items.services`)."""

MAX_TAG_NAME_LENGTH = 50
# Most tags one request may put on a new item.
MAX_TAGS_PER_ITEM = 20


class InvalidTagNameError(Exception):
    pass


def normalize_tag_name(raw: str) -> str:
    """Trims and collapses whitespace ("  machine   learning " ->
    "machine learning"), keeping the user's casing. Raises
    `InvalidTagNameError` if nothing is left or it's too long."""
    name = " ".join(raw.split())
    if not name or len(name) > MAX_TAG_NAME_LENGTH:
        raise InvalidTagNameError()
    return name


def normalize_tag_names(raw_names: list[str]) -> list[str]:
    """`normalize_tag_name` for each, dropping case-insensitive duplicates
    (first spelling wins). Raises `InvalidTagNameError` for any invalid
    name, or more than `MAX_TAGS_PER_ITEM` distinct ones."""
    names: dict[str, str] = {}
    for raw in raw_names:
        name = normalize_tag_name(raw)
        names.setdefault(name.lower(), name)
    if len(names) > MAX_TAGS_PER_ITEM:
        raise InvalidTagNameError()
    return list(names.values())
