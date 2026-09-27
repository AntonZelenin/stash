"""Empty S3 settings mean AWS S3 and the default credential chain (e.g. an
ECS task role), not literal empty strings."""

import boto3
import pytest

from botocore.exceptions import ClientError

from stash_worker_core.errors import PermanentProcessingError, ProcessingLimitExceeded
from stash_worker_core.storage import S3ObjectStore


def _client_kwargs(monkeypatch, **settings) -> dict:
    captured = {}
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: captured.update(kwargs))
    S3ObjectStore(bucket="stash", **settings)
    return captured


def test_empty_settings_use_aws_defaults(monkeypatch):
    kwargs = _client_kwargs(monkeypatch, endpoint_url="", access_key="", secret_key="")

    assert (kwargs["endpoint_url"], kwargs["aws_access_key_id"], kwargs["aws_secret_access_key"]) == (None, None, None)


def test_explicit_settings_are_passed_through(monkeypatch):
    kwargs = _client_kwargs(monkeypatch, endpoint_url="http://minio:9000", access_key="key", secret_key="secret")

    assert (kwargs["endpoint_url"], kwargs["aws_access_key_id"], kwargs["aws_secret_access_key"]) == (
        "http://minio:9000",
        "key",
        "secret",
    )


class _Body:
    def __init__(self, data: bytes):
        self.data = data
        self.closed = False

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


class _S3:
    """Just the calls `S3ObjectStore` makes, over one object."""

    def __init__(self, data: bytes, etag: str = '"e1"'):
        self.data = data
        self.etag = etag
        self.calls: list[dict] = []
        self.bodies: list[_Body] = []

    def _check(self, kwargs):
        self.calls.append(kwargs)
        if "IfMatch" in kwargs and kwargs["IfMatch"] != self.etag:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")

    def get_object(self, **kwargs):
        self._check(kwargs)
        data = self.data
        if "Range" in kwargs:
            start, end = kwargs["Range"].removeprefix("bytes=").split("-")
            data = data[int(start) : int(end) + 1]
        body = _Body(data)
        self.bodies.append(body)
        return {"Body": body, "ContentLength": len(data)}

    def head_object(self, **kwargs):
        self._check(kwargs)
        return {"ContentLength": len(self.data)}


def _store(monkeypatch, data: bytes) -> tuple[S3ObjectStore, _S3]:
    s3 = _S3(data)
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: s3)
    return S3ObjectStore(bucket="stash", endpoint_url="", access_key="", secret_key=""), s3


async def test_download_refuses_an_object_over_the_limit_without_reading_it(monkeypatch):
    store, s3 = _store(monkeypatch, b"x" * 100)

    assert await store.download("k", max_bytes=100) == b"x" * 100
    with pytest.raises(ProcessingLimitExceeded):
        await store.download("k", max_bytes=99)

    assert s3.bodies[-1].closed


async def test_size_and_ranges(monkeypatch):
    store, s3 = _store(monkeypatch, b"0123456789")

    assert await store.size("k") == 10
    assert store.read_range_blocking("k", 2, 5) == b"234"
    assert s3.calls[-1] == {"Bucket": "stash", "Key": "k", "Range": "bytes=2-4"}
    with pytest.raises(ValueError):
        store.read_range_blocking("k", 5, 5)


async def test_reads_pinned_to_an_etag_only_read_that_content(monkeypatch):
    """An original's reads carry its validated ETag (If-Match): anything
    else at the key is permanent, never read or retried."""
    store, s3 = _store(monkeypatch, b"0123456789")

    assert await store.download("k", max_bytes=100, etag='"e1"') == b"0123456789"
    assert await store.size("k", etag='"e1"') == 10
    assert store.read_range_blocking("k", 0, 2, etag='"e1"') == b"01"
    assert all(call["IfMatch"] == '"e1"' for call in s3.calls)

    s3.etag = '"replaced"'
    with pytest.raises(PermanentProcessingError):
        await store.download("k", max_bytes=100, etag='"e1"')
    with pytest.raises(PermanentProcessingError):
        await store.size("k", etag='"e1"')
    with pytest.raises(PermanentProcessingError):
        store.read_range_blocking("k", 0, 2, etag='"e1"')


async def test_unpinned_reads_send_no_condition(monkeypatch):
    """Thumbnails, and legacy originals without a recorded ETag."""
    store, s3 = _store(monkeypatch, b"0123456789")

    await store.download("k", max_bytes=100)

    assert "IfMatch" not in s3.calls[-1]
