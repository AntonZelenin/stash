"""Object storage key layout, shared by the API (uploads) and the workers
(thumbnails), so every object of a user's lives under one prefix:

    users/{user_id}/images/{item_id}{ext}
    users/{user_id}/files/{item_id}{ext}
    users/{user_id}/thumbnails/{item_id}.webp

Keys are built only from ids and a validated extension, never from the
uploaded filename (that's kept in the database, for display and downloads).

The user id in a key is for organizing the bucket (per-user cleanup,
lifecycle rules, usage), *not* for authorization: the bucket is private,
and the only way to an object is a pre-signed URL the API issues for a key
it read from an item the requesting user owns. A key is always read back
from the item's row, never rebuilt from these functions or taken from a
client, so items stored under an older layout keep working."""

from uuid import UUID


def _user_prefix(user_id: UUID) -> str:
    return f"users/{user_id}"


def image_key(user_id: UUID, item_id: UUID, extension: str) -> str:
    """`extension` includes its dot (".png"), as derived from the sniffed
    content type."""
    return f"{_user_prefix(user_id)}/images/{item_id}{extension}"


def file_key(user_id: UUID, item_id: UUID, extension: str) -> str:
    """`extension` includes its dot, or is empty for unrecognized formats."""
    return f"{_user_prefix(user_id)}/files/{item_id}{extension}"


def thumbnail_key(user_id: UUID, item_id: UUID) -> str:
    # Deterministic, so re-running a thumbnail job overwrites the same
    # object instead of leaving extra copies behind.
    return f"{_user_prefix(user_id)}/thumbnails/{item_id}.webp"
