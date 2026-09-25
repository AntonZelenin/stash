"""The S3 storage adapter: settings (empty ones mean AWS S3 and the default
credential chain, e.g. an ECS task role, not literal empty strings), and
the pre-signed URLs and reads the API relies on for direct uploads."""

from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from app.storage.base import StoredObject
from app.storage.minio import MinioStorage


def _clients(monkeypatch, **settings) -> list[dict]:
    created = []

    def fake_client(service, **kwargs):
        created.append(kwargs)
        return object()

    monkeypatch.setattr(boto3, "client", fake_client)
    MinioStorage(bucket="stash", **settings)
    return created


def test_empty_settings_use_aws_defaults_with_one_client(monkeypatch):
    [kwargs] = _clients(monkeypatch, endpoint_url="", public_endpoint_url="", access_key="", secret_key="")

    assert (kwargs["endpoint_url"], kwargs["aws_access_key_id"], kwargs["aws_secret_access_key"]) == (None, None, None)


def test_separate_public_endpoint_gets_its_own_client(monkeypatch):
    internal, public = _clients(
        monkeypatch,
        endpoint_url="http://minio:9000",
        public_endpoint_url="http://localhost:9000",
        access_key="key",
        secret_key="secret",
    )

    assert (internal["endpoint_url"], public["endpoint_url"]) == ("http://minio:9000", "http://localhost:9000")
    assert public["aws_access_key_id"] == "key" and public["aws_secret_access_key"] == "secret"


def _storage() -> MinioStorage:
    return MinioStorage(
        bucket="stash",
        endpoint_url="http://minio:9000",
        public_endpoint_url="http://localhost:9000",
        access_key="key",
        secret_key="secret",
    )


async def test_upload_url_is_signed_for_the_key_type_and_exact_size():
    """So S3 itself rejects an upload to another key, of another type or of
    another size than the API validated."""
    upload = await _storage().generate_upload_url(
        key="users/u/files/i.pdf", content_type="application/pdf", size_bytes=1234, expires_in=900
    )

    url = urlsplit(upload.url)
    query = parse_qs(url.query)
    # Signed against the endpoint the browser can reach.
    assert (url.scheme, url.netloc, url.path) == ("http", "localhost:9000", "/stash/users/u/files/i.pdf")
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-SignedHeaders"] == ["content-length;content-type;host"]
    assert (upload.method, upload.headers) == ("PUT", {"Content-Type": "application/pdf"})


async def test_download_url_can_override_the_stored_content_type():
    url = await _storage().generate_download_url(
        key="users/u/files/i.txt", expires_in=60, content_type="text/plain; charset=utf-8"
    )

    assert parse_qs(urlsplit(url).query)["response-content-type"] == ["text/plain; charset=utf-8"]


async def test_inspect_reads_the_size_and_only_the_first_bytes():
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_response("head_object", {"ContentLength": 50_000_000}, {"Bucket": "stash", "Key": "k"})
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(BytesIO(b"%PDF-"), 5)},
            {"Bucket": "stash", "Key": "k", "Range": "bytes=0-4"},
        )

        stored = await storage.inspect(key="k", head_bytes=5)

    assert stored == StoredObject(size_bytes=50_000_000, head=b"%PDF-")


async def test_inspect_missing_object_is_none():
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)

        assert await storage.inspect(key="k", head_bytes=5) is None


async def test_inspect_other_errors_raise():
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_client_error("head_object", service_error_code="403", http_status_code=403)

        with pytest.raises(ClientError):
            await storage.inspect(key="k", head_bytes=5)
