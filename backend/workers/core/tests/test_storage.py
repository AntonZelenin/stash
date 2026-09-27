"""Empty S3 settings mean AWS S3 and the default credential chain (e.g. an
ECS task role), not literal empty strings."""

import boto3
import pytest

from stash_worker_core.errors import ProcessingLimitExceeded
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

    def __init__(self, data: bytes):
        self.data = data
        self.calls: list[dict] = []
        self.bodies: list[_Body] = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        data = self.data
        if "Range" in kwargs:
            start, end = kwargs["Range"].removeprefix("bytes=").split("-")
            data = data[int(start) : int(end) + 1]
        body = _Body(data)
        self.bodies.append(body)
        return {"Body": body, "ContentLength": len(data)}

    def head_object(self, **kwargs):
        self.calls.append(kwargs)
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
