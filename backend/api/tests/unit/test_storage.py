"""The S3 storage adapter: settings (empty ones mean AWS S3 and the default
credential chain, e.g. an ECS task role, not literal empty strings), and
the pre-signed URLs, reads and copies the API relies on for direct
uploads. Stubbed: what these check is what is asked of S3 and how its
answers are read, not S3 itself (see docs/architecture.md, "Verifying
AWS S3 checksums")."""

import base64
import hashlib
from datetime import UTC, datetime
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from app.storage.base import ListedObject, ObjectChangedError, ObjectListing, StoredObject
from app.storage.s3 import S3Storage


def _clients(monkeypatch, **settings) -> list[dict]:
    created = []

    def fake_client(service, **kwargs):
        created.append(kwargs)
        return object()

    monkeypatch.setattr(boto3, "client", fake_client)
    S3Storage(bucket="stash", **settings)
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


def _storage() -> S3Storage:
    return S3Storage(
        bucket="stash",
        endpoint_url="http://minio:9000",
        public_endpoint_url="http://localhost:9000",
        access_key="key",
        secret_key="secret",
    )


async def test_upload_url_is_signed_for_the_key_type_exact_size_and_create_only():
    """So S3 itself rejects an upload to another key, of another type or of
    another size than the API validated, and a second PUT with the same
    URL (`If-None-Match: *` is signed)."""
    upload = await _storage().generate_upload_url(
        key="uploads/u/i", content_type="application/pdf", size_bytes=1234, expires_in=900
    )

    url = urlsplit(upload.url)
    query = parse_qs(url.query)
    # Signed against the endpoint the browser can reach.
    assert (url.scheme, url.netloc, url.path) == ("http", "localhost:9000", "/stash/uploads/u/i")
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-SignedHeaders"] == ["content-length;content-type;host;if-none-match"]
    assert (upload.method, upload.headers) == ("PUT", {"Content-Type": "application/pdf", "If-None-Match": "*"})


@pytest.mark.parametrize("key", ["users/u/files/i/v.pdf", "users/u/files/i.pdf", "images/i.png"])
async def test_upload_urls_are_never_issued_for_canonical_keys(key):
    with pytest.raises(ValueError):
        await _storage().generate_upload_url(key=key, content_type="application/pdf", size_bytes=1, expires_in=900)


async def test_download_urls_are_never_issued_for_staging_keys():
    with pytest.raises(ValueError):
        await _storage().generate_download_url(key="uploads/u/i", expires_in=60)


async def test_download_url_can_override_the_stored_content_type():
    url = await _storage().generate_download_url(
        key="users/u/files/i.txt", expires_in=60, content_type="text/plain; charset=utf-8"
    )

    assert parse_qs(urlsplit(url).query)["response-content-type"] == ["text/plain; charset=utf-8"]


_SHA256 = hashlib.sha256(b"%PDF-1.7 whole object").digest()
_SHA256_HEX = _SHA256.hex()
_SHA256_B64 = base64.b64encode(_SHA256).decode()


def _head(**fields) -> dict:
    return {"ContentLength": 50_000_000, "ETag": '"e1"', **fields}


_HEAD_PARAMS = {"Bucket": "stash", "Key": "k", "ChecksumMode": "ENABLED"}


async def _inspect(head: dict, *, etag: str = '"e1"') -> StoredObject:
    storage = _storage()
    with Stubber(storage._client) as stubber:
        # ChecksumMode: S3 only returns an object's checksum when asked.
        stubber.add_response("head_object", head, _HEAD_PARAMS)
        # Only from the object HEAD saw.
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(BytesIO(b"%PDF-"), 5)},
            {"Bucket": "stash", "Key": "k", "Range": "bytes=0-4", "IfMatch": etag},
        )
        return await storage.inspect(key="k", head_bytes=5)


async def test_inspect_reads_the_size_and_only_the_first_bytes():
    stored = await _inspect(_head())

    assert stored == StoredObject(size_bytes=50_000_000, head=b"%PDF-", etag='"e1"', sha256=None)


async def test_inspect_returns_the_full_object_sha256_the_storage_keeps():
    stored = await _inspect(_head(ChecksumSHA256=_SHA256_B64, ChecksumType="FULL_OBJECT"))

    assert stored.sha256 == _SHA256_HEX


async def test_a_full_object_sha256_without_a_checksum_type_is_used():
    """Older S3-compatible stores (MinIO included) may not send one; a
    checksum of a single-part object is of the whole object."""
    stored = await _inspect(_head(ChecksumSHA256=_SHA256_B64))

    assert stored.sha256 == _SHA256_HEX


@pytest.mark.parametrize(
    "checksum",
    [
        # A multipart upload's composite checksum: of its parts'
        # checksums, not of the content.
        {"ChecksumSHA256": f"{_SHA256_B64}-3", "ChecksumType": "COMPOSITE"},
        {"ChecksumSHA256": _SHA256_B64, "ChecksumType": "COMPOSITE"},
        {"ChecksumSHA256": f"{_SHA256_B64}-3"},
        # Not a SHA-256 at all.
        {"ChecksumSHA256": base64.b64encode(b"short").decode()},
        {"ChecksumSHA256": "not base64!"},
        # Another algorithm's checksum (S3's default for new objects).
        {"ChecksumCRC64NVME": "AAAAAAAAAAA=", "ChecksumType": "FULL_OBJECT"},
    ],
)
async def test_only_a_valid_full_object_sha256_is_used(checksum):
    stored = await _inspect(_head(**checksum))

    assert stored.sha256 is None


async def test_the_etag_is_an_opaque_token_whatever_its_format():
    """A multipart upload's ETag ("<md5 of part md5s>-<parts>") is no hash
    of the content: it's only passed back as a condition, never
    interpreted."""
    etag = '"0123456789abcdef0123456789abcdef-3"'

    stored = await _inspect(_head(ETag=etag), etag=etag)

    assert stored.etag == etag
    assert stored.sha256 is None


async def test_inspect_of_an_object_replaced_between_head_and_read_fails():
    """The size and the sniffed bytes would describe different content."""
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_response("head_object", {"ContentLength": 10, "ETag": '"e1"'}, _HEAD_PARAMS)
        stubber.add_client_error("get_object", service_error_code="PreconditionFailed", http_status_code=412)

        with pytest.raises(ObjectChangedError):
            await storage.inspect(key="k", head_bytes=5)


_COPY_PARAMS = {
    "Bucket": "stash",
    "Key": "users/u/files/i/v.pdf",
    "CopySource": {"Bucket": "stash", "Key": "uploads/u/i"},
    "CopySourceIfMatch": '"e1"',
    "IfNoneMatch": "*",
    "MetadataDirective": "REPLACE",
    "ContentType": "application/pdf",
    "ChecksumAlgorithm": "SHA256",
}


async def _copy(storage: S3Storage) -> str:
    return await storage.copy_immutable(
        source_key="uploads/u/i", source_etag='"e1"', dest_key="users/u/files/i/v.pdf", content_type="application/pdf"
    )


async def test_copy_is_pinned_create_only_and_checksummed():
    """Only the source state that was inspected, never over an existing
    object, and S3 computes the copy's SHA-256 as it writes it. The copy's
    own ETag is returned as is (not compared with the source's: it's no
    content hash, and differs under some encryption modes)."""
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_response("copy_object", {"CopyObjectResult": {"ETag": '"c1"'}}, _COPY_PARAMS)

        assert await _copy(storage) == '"c1"'


@pytest.mark.parametrize(
    ("code", "status"),
    [
        # The source isn't in the inspected state, or the destination
        # exists (either condition): S3 doesn't say which.
        ("PreconditionFailed", 412),
        ("ConditionalRequestConflict", 409),
        # The source is gone.
        ("NoSuchKey", 404),
    ],
)
async def test_copy_that_fails_a_condition_is_refused_never_adopted(code, status):
    """Nothing already at the destination is ever taken as the copy: it's
    refused, and nothing else is asked of the storage."""
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_client_error("copy_object", service_error_code=code, http_status_code=status)

        with pytest.raises(ObjectChangedError):
            await _copy(storage)
        stubber.assert_no_pending_responses()


async def test_copy_other_errors_raise():
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_client_error("copy_object", service_error_code="AccessDenied", http_status_code=403)

        with pytest.raises(ClientError):
            await _copy(storage)


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


def _listing(keys: list[str], *, truncated: bool) -> dict:
    return {"Contents": [{"Key": key} for key in keys], "IsTruncated": truncated}


def _list_params(max_keys: int = 1000) -> dict:
    return {"Bucket": "stash", "Prefix": "users/u/", "MaxKeys": max_keys}


def _delete_params(keys: list[str]) -> dict:
    return {"Bucket": "stash", "Delete": {"Objects": [{"Key": key} for key in keys], "Quiet": True}}


async def test_delete_prefix_deletes_listed_pages_until_none_are_left():
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_response("list_objects_v2", _listing(["users/u/a", "users/u/b"], truncated=True), _list_params())
        stubber.add_response("delete_objects", {}, _delete_params(["users/u/a", "users/u/b"]))
        stubber.add_response("list_objects_v2", _listing(["users/u/c"], truncated=False), _list_params())
        stubber.add_response("delete_objects", {}, _delete_params(["users/u/c"]))

        assert await storage.delete_prefix(prefix="users/u/", max_objects=5000) is True
        stubber.assert_no_pending_responses()


async def test_delete_prefix_stops_at_max_objects_and_says_more_is_left():
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_response("list_objects_v2", _listing(["users/u/a", "users/u/b"], truncated=True), _list_params(2))
        stubber.add_response("delete_objects", {}, _delete_params(["users/u/a", "users/u/b"]))

        assert await storage.delete_prefix(prefix="users/u/", max_objects=2) is False


async def test_delete_prefix_of_nothing_is_done():
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_response("list_objects_v2", {"IsTruncated": False}, _list_params())

        assert await storage.delete_prefix(prefix="users/u/", max_objects=5000) is True


async def test_delete_prefix_raises_when_some_keys_were_not_deleted():
    """DeleteObjects answers 200 with per-key errors."""
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_response("list_objects_v2", _listing(["users/u/a"], truncated=False), _list_params())
        errors = {"Errors": [{"Key": "users/u/a", "Code": "AccessDenied"}]}
        stubber.add_response("delete_objects", errors, _delete_params(["users/u/a"]))

        with pytest.raises(RuntimeError, match="AccessDenied"):
            await storage.delete_prefix(prefix="users/u/", max_objects=5000)


async def test_list_objects_resumes_after_a_key_and_reads_last_modified():
    storage = _storage()
    modified = datetime(2026, 9, 1, tzinfo=UTC)
    with Stubber(storage._client) as stubber:
        stubber.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "users/u/b", "LastModified": modified}], "IsTruncated": True},
            {**_list_params(1), "StartAfter": "users/u/a"},
        )

        listing = await storage.list_objects(prefix="users/u/", start_after="users/u/a", max_keys=1)

    assert listing == ObjectListing(objects=[ListedObject(key="users/u/b", last_modified=modified)], is_truncated=True)


async def test_list_objects_from_the_start_asks_for_at_most_a_page():
    storage = _storage()
    with Stubber(storage._client) as stubber:
        stubber.add_response("list_objects_v2", {"IsTruncated": False}, _list_params())

        listing = await storage.list_objects(prefix="users/u/", start_after=None, max_keys=5000)

    assert listing == ObjectListing(objects=[], is_truncated=False)
