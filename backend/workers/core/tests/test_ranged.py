"""`RangeReader`: a seekable file over part of a stored object, fetching
only what's read and never more than its limit."""

import io
import zipfile

import pytest

from stash_worker_core.errors import ProcessingLimitExceeded
from stash_worker_core.ranged import RangeReader


class _Object:
    def __init__(self, data: bytes):
        self.data = data
        self.fetches: list[tuple[int, int]] = []

    def fetch(self, start: int, end: int) -> bytes:
        assert 0 <= start < end <= len(self.data)
        self.fetches.append((start, end))
        return self.data[start:end]


def _reader(data: bytes, **kwargs) -> tuple[RangeReader, _Object]:
    obj = _Object(data)
    kwargs.setdefault("max_bytes_read", len(data) * 10)
    return RangeReader(obj.fetch, len(data), **kwargs), obj


def test_reads_like_a_file():
    data = bytes(range(256)) * 100
    reader, _ = _reader(data, block_size=1000)

    assert reader.read(10) == data[:10]
    reader.seek(-5, io.SEEK_END)
    assert reader.read() == data[-5:]
    assert reader.read(3) == b""
    reader.seek(12_345)
    assert reader.read(2_000) == data[12_345:14_345]
    reader.seek(10, io.SEEK_CUR)
    assert reader.tell() == 14_355
    buffer = bytearray(7)
    assert reader.readinto(buffer) == 7 and bytes(buffer) == data[14_355:14_362]


def test_fetches_whole_blocks_once_and_a_run_of_missing_ones_in_one_request():
    reader, obj = _reader(b"x" * 10_000, block_size=1000)

    reader.read(10)
    reader.seek(500)
    reader.read(10)  # same block: cached
    reader.seek(2_500)
    reader.read(3_000)  # blocks 2-5: one request

    assert obj.fetches == [(0, 1000), (2000, 6000)]
    assert reader.bytes_read == 5000


def test_only_the_parts_read_are_fetched_from_a_large_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("media/video.bin", b"\x00" * 5_000_000, compress_type=zipfile.ZIP_STORED)
        archive.writestr("word/document.xml", "<w:document>Contract</w:document>")
    data = buffer.getvalue()
    reader, _ = _reader(data, block_size=64 * 1024)

    with zipfile.ZipFile(reader) as archive:
        assert archive.read("word/document.xml") == b"<w:document>Contract</w:document>"

    assert reader.bytes_read < 200_000 < len(data)


def test_going_over_the_read_limit_raises_before_fetching():
    reader, obj = _reader(b"x" * 10_000, block_size=1000, max_bytes_read=3_000)

    reader.read(2_500)
    with pytest.raises(ProcessingLimitExceeded):
        reader.read()

    assert reader.bytes_read == 3_000 and obj.fetches == [(0, 3000)]


def test_evicted_blocks_are_fetched_again_and_counted_again():
    reader, obj = _reader(b"x" * 10_000, block_size=1000, max_cached_blocks=2)

    for position in (0, 1000, 2000, 0):
        reader.seek(position)
        reader.read(1)

    assert obj.fetches == [(0, 1000), (1000, 2000), (2000, 3000), (0, 1000)]
    assert reader.bytes_read == 4000


def test_the_last_block_may_be_short():
    reader, obj = _reader(b"abcdefghij", block_size=4)

    assert reader.read() == b"abcdefghij"
    assert obj.fetches == [(0, 10)]
