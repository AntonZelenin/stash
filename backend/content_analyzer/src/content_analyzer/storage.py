import asyncio
from abc import ABC, abstractmethod

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from content_analyzer.errors import PermanentProcessingError

_MISSING_OBJECT_CODES = {"NoSuchKey", "404", "NotFound"}


class ImageStore(ABC):
    """Read side of the object storage the API uploads images to."""

    @abstractmethod
    async def download(self, key: str) -> bytes:
        """Raises `PermanentProcessingError` if `key` doesn't exist."""
        ...


class S3ImageStore(ImageStore):
    """S3-compatible storage (MinIO locally, DigitalOcean Spaces in
    production). Deliberately separate from the API's `app.storage`, same
    reasoning as `content_analyzer.items`: the worker only needs a
    download, and shouldn't depend on the API package to get one."""

    def __init__(self, *, endpoint_url: str, access_key: str, secret_key: str, bucket: str):
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4"),
        )

    async def download(self, key: str) -> bytes:
        # boto3 is synchronous; run it off the event loop thread.
        return await asyncio.to_thread(self._download, key)

    def _download(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_OBJECT_CODES:
                raise PermanentProcessingError(f"Object {key!r} not found in storage") from exc
            raise
        return response["Body"].read()
