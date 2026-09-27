"""`download_to_file`: a whole object to local disk, never whole in memory,
and failure categories."""

import tempfile

import pytest
from stash_worker_core.errors import (
    ErrorCategory,
    MalformedInputError,
    PermanentProcessingError,
    ProcessingLimitExceeded,
    error_category,
)
from stash_worker_core.storage import DOWNLOAD_CHUNK_BYTES, download_to_file
from stash_worker_core.testing import FakeObjectStore

_KEY = "users/u/files/big.bin"


async def test_object_is_written_to_the_file_in_chunks(monkeypatch):
    monkeypatch.setattr("stash_worker_core.storage.DOWNLOAD_CHUNK_BYTES", 1000)
    data = bytes(range(256)) * 20  # 5120 bytes: five full chunks and a partial one
    store = FakeObjectStore({_KEY: data})

    with tempfile.TemporaryFile() as file:
        size = await download_to_file(store, _KEY, file, max_bytes=10_000)
        file.seek(0)
        assert file.read() == data

    assert size == len(data)
    assert store.bytes_served[_KEY] == len(data)
    assert store.largest_read[_KEY] == 1000
    assert DOWNLOAD_CHUNK_BYTES == 8 * 1024 * 1024  # the real chunk, patched above


async def test_object_over_the_limit_is_refused_before_any_of_it_is_read():
    store = FakeObjectStore({_KEY: b"x" * 2_000})

    with tempfile.TemporaryFile() as file, pytest.raises(ProcessingLimitExceeded):
        await download_to_file(store, _KEY, file, max_bytes=1_000)

    assert store.bytes_served == {}


async def test_every_read_is_pinned_to_the_etag():
    store = FakeObjectStore({_KEY: b"validated"})
    etag = store.etag_of(b"validated")

    with tempfile.TemporaryFile() as file:
        await download_to_file(store, _KEY, file, max_bytes=100, etag=etag)
        store.objects[_KEY] = b"replaced!"
        with pytest.raises(PermanentProcessingError, match="validated"):
            await download_to_file(store, _KEY, file, max_bytes=100, etag=etag)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (ProcessingLimitExceeded("over"), ErrorCategory.PROCESSING_LIMIT_EXCEEDED),
        (MalformedInputError("corrupt"), ErrorCategory.MALFORMED_INPUT),
        (PermanentProcessingError("missing"), ErrorCategory.PERMANENT),
        (ConnectionError("S3 down"), ErrorCategory.TRANSIENT),
    ],
)
def test_error_categories(error, category):
    assert error_category(error) is category
