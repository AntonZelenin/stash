import asyncio
from abc import ABC, abstractmethod

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from stash_shared import metrics
from stash_shared.log import get_logger

from content_analyzer.errors import PermanentProcessingError

logger = get_logger(__name__)

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
    operations, and shouldn't depend on the API package to get them.

    An empty `endpoint_url` means AWS S3 itself, and empty keys mean boto3's
    default credential chain (e.g. an ECS task role), instead of passing ""
    through. On AWS, missing keys only read as `NoSuchKey` (a permanent
    error) if the role may `s3:ListBucket`; otherwise S3 answers
    `AccessDenied`, which is retried as transient."""

    def __init__(self, *, endpoint_url: str, access_key: str, secret_key: str, bucket: str):
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            config=Config(signature_version="s3v4"),
        )

    # boto3 is synchronous; every call runs off the event loop thread.

    async def download(self, key: str) -> bytes:
        return await self._call("download", key, self._download, key)

    async def upload(self, key: str, data: bytes, *, content_type: str) -> None:
        await self._call(
            "upload",
            key,
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    async def delete(self, key: str) -> None:
        await self._call("delete", key, self._client.delete_object, Bucket=self._bucket, Key=key)

    async def _call(self, operation: str, key: str, function, *args, **kwargs):
        """Runs a boto3 call off the event loop, measured as external call
        `storage.<operation>`, logging a failure with the key involved (the
        worker's retry/dead-letter logs don't know it)."""
        try:
            with metrics.external_call(f"storage.{operation}"):
                return await asyncio.to_thread(function, *args, **kwargs)
        except Exception as exc:
            logger.warning(
                "Storage operation failed",
                operation=operation,
                storage_key=key,
                bucket=self._bucket,
                error_type=type(exc).__name__,
                error_code=_error_code(exc.__cause__ if isinstance(exc, PermanentProcessingError) else exc),
            )
            raise

    def _download(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in _MISSING_OBJECT_CODES:
                raise PermanentProcessingError(f"Object {key!r} not found in storage") from exc
            raise
        return response["Body"].read()


def _error_code(exc: BaseException | None) -> str | None:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code")
    return None
