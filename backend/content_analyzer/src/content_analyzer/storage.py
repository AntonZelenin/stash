import asyncio
from abc import ABC, abstractmethod

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from content_analyzer.errors import PermanentProcessingError

_MISSING_OBJECT_CODES = {"NoSuchKey", "404", "NotFound"}


class ObjectStore(ABC):
    """The object storage the API uploads images to: the workers read
    originals from it and write thumbnails back."""

    @abstractmethod
    async def download(self, key: str) -> bytes:
        """Raises `PermanentProcessingError` if `key` doesn't exist."""
        ...

    @abstractmethod
    async def upload(self, key: str, data: bytes, *, content_type: str) -> None:
        """Creates or overwrites `key` (overwriting is what makes a
        re-run of the same job harmless)."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Deleting a key that doesn't exist is not an error."""
        ...


class S3ObjectStore(ObjectStore):
    """S3-compatible storage (MinIO locally, DigitalOcean Spaces in
    production). Deliberately separate from the API's `app.storage`, same
    reasoning as `content_analyzer.items`: the workers need only these few
    operations, and shouldn't depend on the API package to get them."""

    def __init__(self, *, endpoint_url: str, access_key: str, secret_key: str, bucket: str):
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4"),
        )

    # boto3 is synchronous; every call runs off the event loop thread.

    async def download(self, key: str) -> bytes:
        return await asyncio.to_thread(self._download, key)

    async def upload(self, key: str, data: bytes, *, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object, Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)

    def _download(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_OBJECT_CODES:
                raise PermanentProcessingError(f"Object {key!r} not found in storage") from exc
            raise
        return response["Body"].read()
