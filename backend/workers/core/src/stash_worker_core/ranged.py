"""Reading part of a stored object as if it were a local file.

Some formats keep what a parser needs at known places rather than in one
run from the start: a ZIP's directory is at its end and each member is
found through it, and a PDF's cross-reference table (also at the end) says
where each object is. Parsers for them want a seekable file. `RangeReader`
is one over an object in storage that fetches only the parts actually read,
with Range requests, so a parser that needs the directory and a few members
of a 500 MB archive transfers those, not 500 MB.

It fetches whole blocks (`block_size`), a run of missing blocks in one
request, and keeps the most recent ones (`max_cached_blocks`), since parsers
read small pieces close together and go back to them. Everything it fetches
counts against `max_bytes_read`; going over it raises
`ProcessingLimitExceeded`, however the parser happened to read (a parser
that falls back to reading the whole file, e.g. to recover a damaged one,
stops there too).

Blocking: meant for parsers already running off the event loop.
"""

import io
from collections import OrderedDict
from collections.abc import Callable

from stash_worker_core.errors import ProcessingLimitExceeded

_DEFAULT_BLOCK_SIZE = 256 * 1024
_DEFAULT_CACHED_BLOCKS = 32


class RangeReader(io.RawIOBase):
    def __init__(
        self,
        fetch: Callable[[int, int], bytes],
        size: int,
        *,
        max_bytes_read: int,
        block_size: int = _DEFAULT_BLOCK_SIZE,
        max_cached_blocks: int = _DEFAULT_CACHED_BLOCKS,
    ):
        """`fetch(start, end)`: bytes `start` to `end` (exclusive) of the
        object (e.g. `ObjectStore.read_range_blocking` for one key). `size`:
        the object's size."""
        super().__init__()
        self._fetch = fetch
        self._size = size
        self._max_bytes_read = max_bytes_read
        self._block_size = block_size
        self._max_cached_blocks = max_cached_blocks
        self._blocks: OrderedDict[int, bytes] = OrderedDict()
        self._position = 0
        self.bytes_read = 0
        self.requests = 0

    @property
    def size(self) -> int:
        return self._size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._position + offset
        elif whence == io.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError(f"Invalid whence {whence}")
        if position < 0:
            raise ValueError("Negative seek position")
        self._position = position
        return position

    def read(self, size: int = -1) -> bytes:
        end = self._size if size is None or size < 0 else min(self._size, self._position + size)
        if end <= self._position:
            return b""
        data = self._read(self._position, end)
        self._position = end
        return data

    def readall(self) -> bytes:
        return self.read(-1)

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def _read(self, start: int, end: int) -> bytes:
        first, last = start // self._block_size, (end - 1) // self._block_size
        # This read's blocks, from the cache or fetched (not taken back out
        # of the cache: a large read may evict its own first blocks).
        blocks: dict[int, bytes] = {}
        missing: list[int] = []
        for index in range(first, last + 1):
            cached = self._blocks.get(index)
            if cached is None:
                missing.append(index)
            else:
                self._blocks.move_to_end(index)
                blocks[index] = cached
        for run_first, run_last in _runs(missing):
            blocks.update(self._fetch_blocks(run_first, run_last))

        data = b"".join(blocks[index] for index in range(first, last + 1))
        offset = start - first * self._block_size
        return data[offset : offset + (end - start)]

    def _fetch_blocks(self, first: int, last: int) -> dict[int, bytes]:
        start = first * self._block_size
        end = min(self._size, (last + 1) * self._block_size)
        if self.bytes_read + (end - start) > self._max_bytes_read:
            raise ProcessingLimitExceeded(
                f"Reading bytes {start}-{end} would go over the {self._max_bytes_read}-byte read limit"
            )
        data = self._fetch(start, end)
        self.bytes_read += len(data)
        self.requests += 1
        fetched = {
            index: data[(index - first) * self._block_size : (index - first + 1) * self._block_size]
            for index in range(first, last + 1)
        }
        for index, block in fetched.items():
            self._blocks[index] = block
            self._blocks.move_to_end(index)
        while len(self._blocks) > self._max_cached_blocks:
            self._blocks.popitem(last=False)
        return fetched


def _runs(indexes: list[int]) -> list[tuple[int, int]]:
    """Consecutive runs of sorted `indexes`, as (first, last)."""
    runs: list[tuple[int, int]] = []
    for index in indexes:
        if runs and runs[-1][1] == index - 1:
            runs[-1] = (runs[-1][0], index)
        else:
            runs.append((index, index))
    return runs
