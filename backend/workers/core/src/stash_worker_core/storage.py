import asyncio
from abc import ABC, abstractmethod
from typing import IO

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from stash_shared import metrics
from stash_shared.log import get_logger

from stash_worker_core.errors import PermanentProcessingError, ProcessingLimitExceeded

logger = get_logger(__name__)

# `download_to_file` fetches objects in ranges this large: the most of one
# held in memory at a time.
DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024

_MISSING_OBJECT_CODES = {"NoSuchKey", "404", "NotFound"}
_CHANGED_OBJECT_CODES = {"PreconditionFailed", "412"}


class ObjectStore(ABC):
    """The object storage the API uploads images and files to: the workers
    read originals from it and write thumbnails back.

    Uploads may be far larger than any worker should hold in memory (files
    up to 500 MB), so there's no unbounded read: `download` takes the most
    a caller will accept, and workers that need only part of a file read
    just that part (`size`, `read_range_blocking`; see
    `stash_worker_core.ranged`).

    Every read takes the `etag` the item recorded for its original, if it
    has one: the object is then read only if it's still in the state the
    API recorded (If-Match; the ETag is a change-detection token, not a
    content hash), and anything else is a `PermanentProcessingError`,
    never processed. None (thumbnails, and
    legacy originals that predate recorded ETags) reads whatever is
    there."""

    @abstractmethod
    async def download(self, key: str, *, max_bytes: int, etag: str | None = None) -> bytes:
        """The whole object. Raises `PermanentProcessingError` if `key`
        doesn't exist (or isn't `etag`), and `ProcessingLimitExceeded` if
        it's larger than `max_bytes` — known from its size before any of it
        is read."""
        ...

    @abstractmethod
    async def size(self, key: str, *, etag: str | None = None) -> int:
        """The object's size in bytes, without reading it. Raises
        `PermanentProcessingError` if `key` doesn't exist (or isn't
        `etag`)."""
        ...

    @abstractmethod
    def read_range_blocking(self, key: str, start: int, end: int, *, etag: str | None = None) -> bytes:
        """Bytes `start` to `end` (exclusive) of the object, and only those
        (a Range request). Blocking: for code already running off the event
        loop, like the parsers the analyzers run in a thread. The range
        must be within the object. Raises `PermanentProcessingError` if it
        isn't `etag`."""
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
    """S3-compatible storage (MinIO locally, AWS S3 in
    production). Deliberately separate from the API's `app.storage`, same
    reasoning as `stash_worker_core.items`: the workers need only these few
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

    async def download(self, key: str, *, max_bytes: int, etag: str | None = None) -> bytes:
        return await self._call("download", key, self._download, key, max_bytes, etag)

    async def size(self, key: str, *, etag: str | None = None) -> int:
        return await self._call("head", key, self._size, key, etag)

    def read_range_blocking(self, key: str, start: int, end: int, *, etag: str | None = None) -> bytes:
        return self._call_blocking("read_range", key, self._read_range, key, start, end, etag)

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
        """Runs a boto3 call off the event loop (see `_call_blocking`)."""
        return await asyncio.to_thread(self._call_blocking, operation, key, function, *args, **kwargs)

    def _call_blocking(self, operation: str, key: str, function, *args, **kwargs):
        """Runs a boto3 call, measured as external call `storage.<operation>`,
        logging a failure with the key involved (the worker's
        retry/dead-letter logs don't know it)."""
        try:
            with metrics.external_call(f"storage.{operation}"):
                return function(*args, **kwargs)
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

    def _download(self, key: str, max_bytes: int, etag: str | None) -> bytes:
        response = self._read(key, etag, self._client.get_object, Bucket=self._bucket, Key=key)
        with response["Body"] as body:
            if response["ContentLength"] > max_bytes:
                raise ProcessingLimitExceeded(
                    f"Object {key!r} is {response['ContentLength']} bytes, over the {max_bytes}-byte limit"
                )
            return body.read()

    def _size(self, key: str, etag: str | None) -> int:
        response = self._read(key, etag, self._client.head_object, Bucket=self._bucket, Key=key)
        return response["ContentLength"]

    def _read_range(self, key: str, start: int, end: int, etag: str | None) -> bytes:
        if not 0 <= start < end:
            raise ValueError(f"Invalid range {start}-{end}")
        response = self._read(
            key, etag, self._client.get_object, Bucket=self._bucket, Key=key, Range=f"bytes={start}-{end - 1}"
        )
        with response["Body"] as body:
            return body.read()

    @staticmethod
    def _read(key: str, etag: str | None, function, **kwargs):
        """Runs a read, pinned to `etag` if given. A missing object, or one
        that isn't that content, is permanent: retrying can't change it."""
        if etag is not None:
            kwargs["IfMatch"] = etag
        try:
            return function(**kwargs)
        except ClientError as exc:
            code = _error_code(exc)
            if code in _MISSING_OBJECT_CODES:
                raise PermanentProcessingError(f"Object {key!r} not found in storage") from exc
            if code in _CHANGED_OBJECT_CODES:
                raise PermanentProcessingError(f"Object {key!r} is not the validated content") from exc
            raise


async def download_to_file(
    store: ObjectStore, key: str, file: IO[bytes], *, max_bytes: int, etag: str | None = None
) -> int:
    """Writes the whole object to `file` (e.g. a `tempfile.TemporaryFile`,
    on local disk) in `DOWNLOAD_CHUNK_BYTES` ranges, so never more than one
    range of it is in memory, however large it is. For parsers that need a
    whole file but not in memory. Every range is pinned to `etag`.

    Refused with `ProcessingLimitExceeded`, before any of it is read, if
    it's over `max_bytes`: bounds the local disk used. Returns its size."""
    size = await store.size(key, etag=etag)
    if size > max_bytes:
        raise ProcessingLimitExceeded(f"Object {key!r} is {size} bytes, over the {max_bytes}-byte limit")
    for start in range(0, size, DOWNLOAD_CHUNK_BYTES):
        end = min(size, start + DOWNLOAD_CHUNK_BYTES)
        file.write(await asyncio.to_thread(store.read_range_blocking, key, start, end, etag=etag))
    return size


def _error_code(exc: BaseException | None) -> str | None:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code")
    return None
