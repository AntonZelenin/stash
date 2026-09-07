import asyncio
from functools import lru_cache

import boto3
from botocore.client import Config

from app.config import get_settings
from app.storage.base import ObjectStorage


class MinioStorage(ObjectStorage):
    """Object storage backed by MinIO's S3-compatible API."""

    def __init__(self, *, endpoint_url: str, access_key: str, secret_key: str, bucket: str):
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4"),
        )

    async def upload(self, *, key: str, data: bytes, content_type: str) -> None:
        # boto3 is synchronous; run it off the event loop thread.
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )


@lru_cache
def get_object_storage() -> ObjectStorage:
    settings = get_settings()
    return MinioStorage(
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket=settings.s3_bucket,
    )
