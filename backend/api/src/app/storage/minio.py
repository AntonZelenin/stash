import asyncio
from functools import lru_cache
from urllib.parse import quote

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from stash_shared import metrics
from stash_shared.log import get_logger

from app.config import get_settings
from app.storage.base import ObjectStorage, PresignedUpload, StoredObject

logger = get_logger(__name__)


class MinioStorage(ObjectStorage):
    """Object storage backed by MinIO's S3-compatible API.

    The API's own calls go through `endpoint_url` — reachable
    server-to-server (e.g. the `minio` Docker-network hostname locally).
    Pre-signed URLs (uploads and downloads) are signed against
    `public_endpoint_url` instead: they're handed to the browser,
    which can't resolve an internal Docker hostname, so they need whatever
    endpoint is actually reachable from outside the network (e.g.
    `localhost` locally; the same public endpoint in production, where
    there's no internal/external split).

    Empty endpoints mean AWS S3 itself, and empty keys mean boto3's default
    credential chain (e.g. an ECS task role), instead of passing "" through.
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
        self._client = _s3_client(endpoint_url, access_key, secret_key)
        self._public_client = (
            self._client
            if (public_endpoint_url or None) == (endpoint_url or None)
            else _s3_client(public_endpoint_url, access_key, secret_key)
        )

    async def generate_upload_url(
        self, *, key: str, content_type: str, size_bytes: int, expires_in: int
    ) -> PresignedUpload:
        # Content-Type and Content-Length become signed headers: S3 rejects
        # a PUT with any other type or size (the browser sets the length
        # from the body itself).
        url = await asyncio.to_thread(
            self._public_client.generate_presigned_url,
            "put_object",
            Params={"Bucket": self._bucket, "Key": key, "ContentType": content_type, "ContentLength": size_bytes},
            ExpiresIn=expires_in,
        )
        return PresignedUpload(url=url, method="PUT", headers={"Content-Type": content_type})

    async def inspect(self, *, key: str, head_bytes: int) -> StoredObject | None:
        try:
            with metrics.external_call("storage.inspect"):
                return await asyncio.to_thread(self._inspect, key, head_bytes)
        except Exception as exc:
            # Re-raised: the request fails. Logged here for the key and the
            # S3 error code, which the request's own error log lacks.
            logger.warning(
                "Storage inspect failed",
                storage_key=key,
                bucket=self._bucket,
                error_type=type(exc).__name__,
                error_code=_error_code(exc),
            )
            raise

    def _inspect(self, key: str, head_bytes: int) -> StoredObject | None:
        try:
            size = self._client.head_object(Bucket=self._bucket, Key=key)["ContentLength"]
        except ClientError as exc:
            # HEAD responses have no body, so a missing key is just "404".
            if _error_code(exc) in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        head = b""
        if size > 0 and head_bytes > 0:
            # A range, so a 50 MB upload costs the API only a few KB.
            response = self._client.get_object(Bucket=self._bucket, Key=key, Range=f"bytes=0-{head_bytes - 1}")
            head = response["Body"].read()
        return StoredObject(size_bytes=size, head=head)

    async def delete(self, *, key: str) -> None:
        # S3 DeleteObject already succeeds for a missing key.
        with metrics.external_call("storage.delete"):
            await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def generate_download_url(
        self,
        *,
        key: str,
        expires_in: int,
        filename: str | None = None,
        inline: bool = True,
        content_type: str | None = None,
    ) -> str:
        params = {"Bucket": self._bucket, "Key": key}
        if content_type is not None:
            params["ResponseContentType"] = content_type
        if filename is not None:
            # Signed into the URL, so S3 sends it back as the response's
            # Content-Disposition header.
            params["ResponseContentDisposition"] = _content_disposition(filename, inline=inline)
        return await asyncio.to_thread(
            self._public_client.generate_presigned_url,
            "get_object",
            Params=params,
            ExpiresIn=expires_in,
        )


def _content_disposition(filename: str, *, inline: bool) -> str:
    """`inline` lets browsers display what they can (PDFs, text);
    `attachment` always downloads. The name goes in twice, per RFC 6266: an
    ASCII-only fallback plus the exact UTF-8 name (`filename*`), which modern
    browsers use."""
    disposition = "inline" if inline else "attachment"
    ascii_fallback = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _error_code(exc: Exception) -> str | None:
    return exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else None


def _s3_client(endpoint_url: str, access_key: str, secret_key: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or None,
        aws_access_key_id=access_key or None,
        aws_secret_access_key=secret_key or None,
        config=Config(signature_version="s3v4"),
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
