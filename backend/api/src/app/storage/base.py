from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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


class ObjectStorage(ABC):
    """Storage for uploaded file content, keyed by an opaque storage key.

    Backed by an S3-compatible bucket (MinIO locally, AWS S3 in
    production). Item/business logic depends only on this interface so the
    concrete backend can be swapped without touching it.

    The API never writes uploaded content itself: clients upload straight
    to storage with a URL from `generate_upload_url`, and the API only
    `inspect`s the result.
    """

    @abstractmethod
    async def generate_upload_url(
        self, *, key: str, content_type: str, size_bytes: int, expires_in: int
    ) -> PresignedUpload:
        """Returns a temporary, pre-signed upload for exactly `key`. The
        signature also binds the content type and the exact size, so the
        storage itself rejects anything else — a client can't use it for
        another key, or to store more than it declared."""
        ...

    @abstractmethod
    async def inspect(self, *, key: str, head_bytes: int) -> StoredObject | None:
        """The object's size and up to `head_bytes` of its beginning (for
        sniffing its type), without downloading the rest. None if there is
        no object at `key`."""
        ...

    @abstractmethod
    async def delete(self, *, key: str) -> None:
        """Removes `key`. Deleting a key that doesn't exist is not an error."""
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
        """
        ...
