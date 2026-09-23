import asyncio
from functools import lru_cache

import boto3
from botocore.client import Config

from app.config import get_settings
from app.storage.base import ObjectStorage


class MinioStorage(ObjectStorage):
    """Object storage backed by MinIO's S3-compatible API.

    Uploads go through `endpoint_url` — reachable server-to-server (e.g. the
    `minio` Docker-network hostname locally). Pre-signed URLs are signed
    against `public_endpoint_url` instead: they're handed to the browser,
    which can't resolve an internal Docker hostname, so they need whatever
    endpoint is actually reachable from outside the network (e.g.
    `localhost` locally; the same public endpoint in production, where
    there's no internal/external split).
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        public_endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
    ):
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4"),
        )
        self._public_client = (
            self._client
            if public_endpoint_url == endpoint_url
            else boto3.client(
                "s3",
                endpoint_url=public_endpoint_url,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(signature_version="s3v4"),
            )
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

    async def delete(self, *, key: str) -> None:
        # S3 DeleteObject already succeeds for a missing key.
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def generate_download_url(self, *, key: str, expires_in: int) -> str:
        return await asyncio.to_thread(
            self._public_client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )


@lru_cache
def get_object_storage() -> ObjectStorage:
    settings = get_settings()
    return MinioStorage(
        endpoint_url=settings.s3_endpoint_url,
        public_endpoint_url=settings.s3_public_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket=settings.s3_bucket,
    )
