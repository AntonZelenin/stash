import asyncio
import base64
import binascii
from functools import lru_cache
from urllib.parse import quote

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from stash_shared import metrics, storage_keys
from stash_shared.log import get_logger

from app.config import get_settings
from app.storage.base import ObjectChangedError, ObjectStorage, PresignedUpload, StoredObject

logger = get_logger(__name__)

_MISSING_CODES = ("404", "NoSuchKey", "NotFound")
# A conditional request's condition didn't hold (412), or another
# conditional write to the same key was in flight (409).
_CONDITION_FAILED_CODES = ("PreconditionFailed", "412", "ConditionalRequestConflict")


class S3Storage(ObjectStorage):
    """Object storage backed by an S3-compatible API (AWS S3, MinIO, ...).

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
        if not storage_keys.is_staging_key(key):
            # Canonical objects are only ever created by `copy_immutable`
            # (the bucket policy refuses pre-signed writes there too).
            raise ValueError("Upload URLs are only issued for staging keys")
        # Content-Type, Content-Length and If-None-Match become signed
        # headers: S3 rejects a PUT with any other type or size (the browser
        # sets the length from the body itself), and, with `If-None-Match:
        # *`, one to a key that already has an object, so the URL can't
        # replace what was uploaded with it.
        url = await asyncio.to_thread(
            self._public_client.generate_presigned_url,
            "put_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ContentType": content_type,
                "ContentLength": size_bytes,
                "IfNoneMatch": "*",
            },
            ExpiresIn=expires_in,
        )
        return PresignedUpload(url=url, method="PUT", headers={"Content-Type": content_type, "If-None-Match": "*"})

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
            # ChecksumMode: include the object's stored checksum, if any.
            response = self._client.head_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as exc:
            # HEAD responses have no body, so a missing key is just "404".
            if _error_code(exc) in _MISSING_CODES:
                return None
            raise
        size, etag, sha256 = response["ContentLength"], response["ETag"], _full_object_sha256(response)
        head = b""
        if size > 0 and head_bytes > 0:
            # A range, so a 50 MB upload costs the API only a few KB. Only
            # of the object HEAD saw (If-Match), so the size and the bytes
            # sniffed describe the same state of the object: the ETag
            # they're validated under (a token, not a content hash).
            try:
                response = self._client.get_object(
                    Bucket=self._bucket, Key=key, Range=f"bytes=0-{head_bytes - 1}", IfMatch=etag
                )
            except ClientError as exc:
                if _error_code(exc) in _CONDITION_FAILED_CODES + _MISSING_CODES:
                    raise ObjectChangedError(key) from exc
                raise
            head = response["Body"].read()
        return StoredObject(size_bytes=size, head=head, etag=etag, sha256=sha256)

    async def copy_immutable(self, *, source_key: str, source_etag: str, dest_key: str, content_type: str) -> str:
        with metrics.external_call("storage.copy"):
            return await asyncio.to_thread(self._copy_immutable, source_key, source_etag, dest_key, content_type)

    def _copy_immutable(self, source_key: str, source_etag: str, dest_key: str, content_type: str) -> str:
        try:
            response = self._client.copy_object(
                Bucket=self._bucket,
                Key=dest_key,
                CopySource={"Bucket": self._bucket, "Key": source_key},
                # Only the object state that was validated...
                CopySourceIfMatch=source_etag,
                # ...and never over an existing object. (MinIO ignores this
                # on a copy; `dest_key` is random and new, so there's none.)
                IfNoneMatch="*",
                MetadataDirective="REPLACE",
                ContentType=content_type,
                # S3 computes the copy's full-object SHA-256 from the bytes
                # it writes (a CopyObject result is never multipart), and
                # keeps it with the object: `inspect` reads it back.
                ChecksumAlgorithm="SHA256",
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code not in _CONDITION_FAILED_CODES + _MISSING_CODES:
                raise
            logger.warning(
                "Immutable copy refused",
                source_key=source_key,
                storage_key=dest_key,
                bucket=self._bucket,
                error_code=code,
            )
            raise ObjectChangedError(source_key) from exc
        return response["CopyObjectResult"]["ETag"]

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
        if storage_keys.is_staging_key(key):
            # Unvalidated, and never an item's content.
            raise ValueError("Staging objects are never served")
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


def _full_object_sha256(head: dict) -> str | None:
    """The hex SHA-256 of the whole object from a HEAD with
    `ChecksumMode`, if S3 keeps one. A multipart upload's composite
    checksum (a checksum of the parts' checksums, "<base64>-<parts>") is
    not the content's digest, so it's ignored."""
    value = head.get("ChecksumSHA256")
    if not value or head.get("ChecksumType", "FULL_OBJECT") != "FULL_OBJECT":
        return None
    try:
        digest = base64.b64decode(value, validate=True)
    except binascii.Error:
        return None
    return digest.hex() if len(digest) == 32 else None


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
    return S3Storage(
        endpoint_url=settings.s3_endpoint_url,
        public_endpoint_url=settings.s3_public_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket=settings.s3_bucket,
    )
