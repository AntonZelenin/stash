"""Object storage key layout, shared by the API (uploads) and the workers
(thumbnails). Two disjoint areas:

    uploads/{user_id}/{upload_id}                        staging (mutable)
    users/{user_id}/images/{item_id}/{object_id}{ext}    canonical original
    users/{user_id}/files/{item_id}/{object_id}{ext}     canonical original
    users/{user_id}/thumbnails/{item_id}.webp            derived by the worker

Clients only ever write to a staging key, with a pre-signed single PUT.
Finalizing an upload validates the staging object and copies it, in the
state it was validated in (pinned by its ETag: a concurrency token, not a
content hash), to a fresh canonical key, which is create-only and never
written again;
items, workers and downloads only ever use that key. The bucket policy
refuses pre-signed writes outside `uploads/`, and a lifecycle rule expires
everything under `uploads/`, never anything else.

`object_id` is random (`new_object_id`), new for every copy: a canonical
key names one stored object, not its content (the content's digest is kept
in the database, see the API's `content_sha256`). Nothing relies on it
being unguessable: pre-signed URLs can't write there at all.

Keys are built only from ids, a validated extension and the random
object id, never from the uploaded filename (that's kept in the database, for
display and downloads).

The user id in a key is for organizing the bucket (per-user cleanup,
lifecycle rules, usage), *not* for authorization: the bucket is private,
and the only way to an object is a pre-signed URL the API issues for a key
it read from an item the requesting user owns. A key is always read back
from the item's row, never rebuilt from these functions or taken from a
client, so items stored under an older layout (`users/{user_id}/images/
{item_id}{ext}`, or unscoped `images/...`) keep working."""

from uuid import UUID, uuid4

STAGING_PREFIX = "uploads/"

# Every thumbnail is WebP (see `thumbnail_key`).
THUMBNAIL_CONTENT_TYPE = "image/webp"


def _user_prefix(user_id: UUID) -> str:
    return f"users/{user_id}"


def staging_key(user_id: UUID, upload_id: UUID) -> str:
    """Where a client uploads to, before finalize. Never an item's key."""
    return f"{STAGING_PREFIX}{user_id}/{upload_id}"


def is_staging_key(key: str) -> bool:
    return key.startswith(STAGING_PREFIX)


def user_prefixes(user_id: UUID) -> list[str]:
    """Every prefix the user's objects are stored under: their canonical
    area and their staging one. Deleting the account deletes both."""
    return [f"{_user_prefix(user_id)}/", f"{STAGING_PREFIX}{user_id}/"]


def new_object_id() -> str:
    """A fresh, key-safe name for one canonical object: 128 random bits,
    so two copies never share a key."""
    return uuid4().hex


def image_key(user_id: UUID, item_id: UUID, object_id: str, extension: str) -> str:
    """`extension` includes its dot (".png"), as derived from the sniffed
    content type. `object_id` is from `new_object_id`."""
    return f"{_user_prefix(user_id)}/images/{item_id}/{object_id}{extension}"


def file_key(user_id: UUID, item_id: UUID, object_id: str, extension: str) -> str:
    """`extension` includes its dot, or is empty for unrecognized formats."""
    return f"{_user_prefix(user_id)}/files/{item_id}/{object_id}{extension}"


def thumbnail_key(user_id: UUID, item_id: UUID) -> str:
    # Deterministic, so re-running a thumbnail job overwrites the same
    # object instead of leaving extra copies behind.
    return f"{_user_prefix(user_id)}/thumbnails/{item_id}.webp"
