from abc import ABC, abstractmethod


class ObjectStorage(ABC):
    """Storage for uploaded file content, keyed by an opaque storage key.

    Backed by an S3-compatible bucket (MinIO locally, DigitalOcean Spaces in
    production). Item/business logic depends only on this interface so the
    concrete backend can be swapped without touching it.
    """

    @abstractmethod
    async def upload(self, *, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    async def generate_download_url(self, *, key: str, expires_in: int) -> str:
        """Returns a temporary, pre-signed URL the client can fetch `key`
        from directly, without proxying the bytes through the API."""
        ...
