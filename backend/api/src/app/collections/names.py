"""Collection-name rules, shared by adding existing items to collections
(`app.collections.services`) and adding items as they're created
(`app.items.services`)."""

MAX_COLLECTION_NAME_LENGTH = 50
# Most collections one request may put a new item in.
MAX_COLLECTIONS_PER_ITEM = 20


class InvalidCollectionNameError(Exception):
    pass


def normalize_collection_name(raw: str) -> str:
    """Trims and collapses whitespace, keeping the user's casing. Raises
    `InvalidCollectionNameError` if nothing is left or it's too long."""
    name = " ".join(raw.split())
    if not name or len(name) > MAX_COLLECTION_NAME_LENGTH:
        raise InvalidCollectionNameError()
    return name


def normalize_collection_names(raw_names: list[str]) -> list[str]:
    """`normalize_collection_name` for each, dropping case-insensitive
    duplicates (first spelling wins). Raises `InvalidCollectionNameError`
    for any invalid name, or more than `MAX_COLLECTIONS_PER_ITEM` distinct
    ones."""
    names: dict[str, str] = {}
    for raw in raw_names:
        name = normalize_collection_name(raw)
        names.setdefault(name.lower(), name)
    if len(names) > MAX_COLLECTIONS_PER_ITEM:
        raise InvalidCollectionNameError()
    return list(names.values())
