from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class PresignedUpload:
    """Lets a client store one object directly, without the bytes going
    through the API: send the content to `url` with `method`, setting
    exactly `headers` (they're part of the signature)."""

    url: str
    method: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredObject:
    size_bytes: int
    # The object's first bytes, up to what was asked for.
    head: bytes
    # A concurrency token for this state of the object, not a content
    # hash (S3's ETag is an MD5 only for some objects: not for multipart
    # uploads, nor under SSE-KMS). `size_bytes` and `head` are both read
    # from the object while it has this ETag, and `copy_immutable` copies
    # only an object that still has it.
    etag: str
    # Hex SHA-256 of the whole object, as computed by the storage itself,
    # if it keeps one: `copy_immutable` makes it compute one for every
    # copy. None if it has none (e.g. a staging object).
    sha256: str | None = None


@dataclass(frozen=True)
class ListedObject:
    key: str
    # When the object was last written, by the storage's clock.
    last_modified: datetime


@dataclass(frozen=True)
class ObjectListing:
    # In key order.
    objects: list[ListedObject]
    # Whether there are more keys after the last one listed.
    is_truncated: bool


class ObjectChangedError(Exception):
    """The object isn't (or is no longer) the content it was expected to
    be: it was replaced after it was inspected."""


class ObjectStorage(ABC):
    """Storage for uploaded file content, keyed by an opaque storage key.

    Backed by an S3-compatible bucket (MinIO locally, AWS S3 in
    production). Item/business logic depends only on this interface so the
    concrete backend can be swapped without touching it.

    The API never writes uploaded content itself: clients upload straight
    to a staging key (`stash_shared.storage_keys.staging_key`) with a URL
    from `generate_upload_url` (one single PUT; S3 Multipart Upload isn't
    used); the API `inspect`s the result and, if it's valid,
    `copy_immutable`s it, in that inspected state, to a fresh canonical
    key, then `inspect`s the copy (what was validated, plus its SHA-256).
    That key is what the item references from then on.
    """

    @abstractmethod
    async def generate_upload_url(
        self, *, key: str, content_type: str, size_bytes: int, expires_in: int
    ) -> PresignedUpload:
        """Returns a temporary, pre-signed upload for exactly `key`, which
        must be a staging key. The signature also binds the content type,
        the exact size and create-only semantics (`If-None-Match: *`), so
        the storage itself rejects anything else — a client can't use it
        for another key, to store more than it declared, or to replace
        what it already uploaded."""
        ...

    @abstractmethod
    async def inspect(self, *, key: str, head_bytes: int) -> StoredObject | None:
        """The object's size, ETag, SHA-256 (if the storage has one) and up
        to `head_bytes` of its beginning (for sniffing its type), without
        downloading the rest. None if
        there is no object at `key`. Raises `ObjectChangedError` if the
        object was replaced while it was being read (so the size and the
        bytes would describe different content)."""
        ...

    @abstractmethod
    async def copy_immutable(self, *, source_key: str, source_etag: str, dest_key: str, content_type: str) -> str:
        """Server-side copies the object at `source_key` to `dest_key`
        (a fresh key), only if the source still has `source_etag` (the
        state that was inspected), and only if `dest_key` doesn't exist
        yet. The copy is stored with `content_type`, and the storage
        computes its SHA-256 as it writes it (see `StoredObject.sha256`).
        Returns the copy's ETag.

        Raises `ObjectChangedError` if the source isn't in that state any
        more (or is gone), or `dest_key` already exists."""
        ...

    @abstractmethod
    async def delete(self, *, key: str) -> None:
        """Removes `key`. Deleting a key that doesn't exist is not an error."""
        ...

    @abstractmethod
    async def delete_prefix(self, *, prefix: str, max_objects: int) -> bool:
        """Removes objects whose key starts with `prefix`, at most
        `max_objects` of them per call, so one call stays bounded however
        much is stored there. Returns whether nothing is left under
        `prefix`; if something is, call again. Raises if a delete fails."""
        ...

    @abstractmethod
    async def list_objects(self, *, prefix: str, start_after: str | None, max_keys: int) -> ObjectListing:
        """Up to `max_keys` objects whose key starts with `prefix`, in key
        order, starting after the key `start_after` (from the first one if
        None): one page of a scan that can be resumed later from the last
        key it saw."""
        ...

    @abstractmethod
    async def generate_download_url(
        self,
        *,
        key: str,
        expires_in: int,
        filename: str | None = None,
        inline: bool = True,
        content_type: str | None = None,
    ) -> str:
        """Returns a temporary, pre-signed URL the client can fetch `key`
        from directly, without proxying the bytes through the API.

        With `filename`, the response names the file for saving, and tells
        the browser to display it (`inline`, where it can) or always
        download it (`inline=False`). With `content_type`, the response
        carries that type instead of the one the object was stored with
        (e.g. the validated type, with its charset).

        Refuses (`ValueError`) a staging key: those are never served.
        """
        ...
