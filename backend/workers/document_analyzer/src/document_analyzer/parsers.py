"""Plain-text extraction from uploaded documents, within processing limits.

Each supported format has a `DocumentParser`, looked up by the content type
the API stored for the file (see `app.items.files` on the API side, whose
`analyzable` formats are exactly the ones registered here). To support a new
format, implement `DocumentParser` and add it to `_PARSERS`.

Files may be uploaded up to 500 MB, but describing one takes a few
thousand characters of its text, so no parser reads or unpacks more than
`ParserLimits` allow, however large or hostile the file:

- Parsers read from a `DocumentSource`, which fetches only the byte ranges
  asked for. Formats read from the start (HTML, XML, FB2, RTF) read at
  most `max_prefix_bytes`. Plain text over `max_prefix_bytes` is read only
  where the excerpts come from. ZIP-based formats, whose directory is at
  the end, read through a seekable `RangeReader`: the directory and the
  members they parse, at most `max_bytes_read` in all, never media.
- PDFs are the exception: they need random access to the whole file.
  Objects can be anywhere, and pypdf checks the header of every object
  the cross-reference table lists as it opens one (to recover damaged
  files), so ranged reads would fetch most of it anyway. A PDF is
  downloaded (up to `max_download_bytes`) to an anonymous temporary file
  on local disk, never into memory, and parsed from there; pypdf reads at
  most `max_bytes_read` of it.
- ZIP-based formats (DOCX/XLSX/PPTX, OpenDocument, EPUB, FB2.ZIP): at most
  `max_archive_entries` entries (as declared, and as listed), and
  `max_uncompressed_bytes` decompressed, all members together, counted as
  they're inflated (not as declared). Each member inflates at most
  `max_member_bytes`, and at most `max_compression_ratio` times its
  compressed size (past `_RATIO_GRACE_BYTES`): caps set from its directory
  entry before inflating it, like the budget ending extraction when
  reached rather than refusing the member outright, so a document's text
  up to there is kept. Encrypted members are refused, and an archive
  listing a member outside itself (absolute, `..`: zip-slip) isn't a
  document. Members are only ever read in memory, never extracted to disk.
- PDFs: at most `max_pdf_page_count` pages in all (pypdf lists every page
  as it opens a document), of which `max_pdf_pages` are read (the first
  half of the budget from the start, the rest spread over the remainder).
  Opening a PDF (cross-reference data, page tree) reads and decompresses
  at most `max_pdf_open_bytes`: pypdf holds it at ~10x its size.
  A page's text is extracted only if its content streams (its own and
  those of the form XObjects it draws, each time it draws one) come to at
  most `max_pdf_page_content_bytes`: pypdf parses each in one go, which
  can't be interrupted and costs ~40x its size in memory; a larger page is
  skipped. At most `max_uncompressed_bytes` decompressed, all streams
  together (content, fonts, cross-reference data...), and at most
  `max_pdf_stream_bytes` by any one (pypdf's own limits). See `_PdfBudget`.
- XML (in any format) is parsed as a stream: text is collected as elements
  end and each element is dropped once read, so memory doesn't grow with
  the document. Entity declarations are refused (defusedxml). At most
  `max_xml_elements` elements, all parts together.
- At most `max_text_chars` of text are extracted, and parsing stops after
  `timeout_seconds`: checked between pages, members, chunks, every
  thousand XML elements and every thousand PDF operators. One call into a
  parsing library can still overrun it (by at most one PDF page's content,
  one HTML chunk or one RTF prefix); the function's timeout is the final
  backstop.

A limit reached after some text was extracted stops there: that text is
described, marked partial. Reached before any, it raises
`ProcessingLimitExceeded` (permanent). The upload itself stays either way.

Parsers are synchronous and CPU-bound — call them off the event loop. They
raise on input they can't parse (corrupt, encrypted, truncated...); parsing
is deterministic, so callers should treat that as permanent.

pypdf is instrumented for this, process-wide, but only counts while a
`PdfParser` is extracting, in its thread (see `_PdfBudget`).
"""

import codecs
import io
import itertools
import os
import posixpath
import re
import struct
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import IO, Protocol

import charset_normalizer
import pypdf
import pypdf.filters
from defusedxml import ElementTree
from pypdf import PdfReader
from pypdf.errors import LimitReachedError
from pypdf.generic import ContentStream
from stash_worker_core.errors import MalformedInputError, ProcessingLimitExceeded
from stash_worker_core.ranged import RangeReader
from striprtf.striprtf import rtf_to_text

from document_analyzer.excerpt import excerpt_from_ranges


class UnparsableDocumentError(MalformedInputError):
    """The document's structure is invalid or unsupported (as opposed to a
    low-level library error, which callers treat the same way)."""


@dataclass(frozen=True)
class ParserLimits:
    """How much work extracting one document's text may take. Independent
    of (and far below) the upload size limit; see the module docstring.
    Defaults are the worker's settings' defaults."""

    max_prefix_bytes: int = 8 * 1024 * 1024
    max_bytes_read: int = 64 * 1024 * 1024
    max_download_bytes: int = 512 * 1024 * 1024
    max_uncompressed_bytes: int = 64 * 1024 * 1024
    max_archive_entries: int = 10_000
    max_member_bytes: int = 64 * 1024 * 1024
    max_compression_ratio: int = 200
    max_xml_elements: int = 2_000_000
    max_pdf_pages: int = 50
    max_pdf_page_count: int = 10_000
    max_pdf_open_bytes: int = 8 * 1024 * 1024
    max_pdf_stream_bytes: int = 16 * 1024 * 1024
    max_pdf_page_content_bytes: int = 2 * 1024 * 1024
    max_text_chars: int = 240_000
    timeout_seconds: float = 60.0
    # Plain text too large to read whole: characters of excerpts read from
    # it (the describer's `max_chars`).
    excerpt_chars: int = 24_000


@dataclass(frozen=True)
class ExtractedText:
    text: str
    # Only part of the document was extracted (a limit was reached, or
    # only excerpts or some pages were read).
    is_partial: bool
    # The limit that stopped extraction, for logs; None if none did.
    stopped_by: str | None = None


class DocumentSource:
    """The file being parsed: its size, and reads of just the parts a
    parser asks for. `fetch(start, end)` reads bytes `start` to `end`
    (exclusive), e.g. `ObjectStore.read_range_blocking` for one key.

    A failure to fetch is kept in `storage_error`, so the caller can tell
    it (and retry it) from a document that can't be parsed, even if a
    parser caught it."""

    def __init__(self, fetch: Callable[[int, int], bytes], size: int):
        self._fetch = fetch
        self.size = size
        self.storage_error: Exception | None = None
        self._direct_bytes = 0
        self._readers: list[RangeReader] = []

    @classmethod
    def from_bytes(cls, data: bytes) -> "DocumentSource":
        return cls(lambda start, end: data[start:end], len(data))

    @property
    def bytes_read(self) -> int:
        """Bytes fetched so far, by every kind of read."""
        return self._direct_bytes + sum(reader.bytes_read for reader in self._readers)

    def read(self, start: int, end: int) -> bytes:
        end = min(end, self.size)
        if start >= end:
            return b""
        data = self.fetch(start, end)
        self._direct_bytes += len(data)
        return data

    def prefix(self, max_bytes: int) -> tuple[bytes, bool]:
        """The file's first `max_bytes` at most, and whether that's less
        than all of it."""
        return self.read(0, max_bytes), self.size > max_bytes

    def open(self, *, max_bytes_read: int) -> RangeReader:
        """A seekable file over the whole document that fetches only what's
        read, at most `max_bytes_read` in all."""
        reader = RangeReader(self.fetch, self.size, max_bytes_read=max_bytes_read)
        self._readers.append(reader)
        return reader

    @contextmanager
    def downloaded(self, *, max_bytes: int, check_time: Callable[[], None]) -> Iterator[IO[bytes]]:
        """The whole document, downloaded to a temporary file on local disk
        (deleted afterwards) in `_DOWNLOAD_CHUNK_BYTES` ranges: only one of
        them in memory at a time. Refused, before downloading anything, if
        it's over `max_bytes`."""
        if self.size > max_bytes:
            raise ProcessingLimitExceeded(f"File is {self.size} bytes, over the {max_bytes}-byte download limit")
        with tempfile.TemporaryFile() as file:
            for start in range(0, self.size, _DOWNLOAD_CHUNK_BYTES):
                check_time()
                file.write(self.read(start, start + _DOWNLOAD_CHUNK_BYTES))
            file.seek(0)
            yield file

    def fetch(self, start: int, end: int) -> bytes:
        try:
            return self._fetch(start, end)
        except Exception as exc:
            self.storage_error = exc
            raise


# A download to local disk is fetched in ranges this large.
_DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024


class _TextFull(ProcessingLimitExceeded):
    pass


class TextCollector:
    """Where parsers put the text they extract: at most `max_text_chars`
    of it, within `timeout_seconds` of starting. `add` raises
    `ProcessingLimitExceeded` once the text is full (keeping what fits);
    `check_time` once time is up. Parsers set `partial` when they skip part
    of a document on their own (pages, a truncated prefix)."""

    def __init__(self, limits: ParserLimits, *, clock: Callable[[], float] = time.monotonic):
        self._max_chars = limits.max_text_chars
        self._max_elements = limits.max_xml_elements
        self._elements = 0
        self._clock = clock
        self._deadline = clock() + limits.timeout_seconds
        self._parts: list[str] = []
        self._chars = 0
        self.partial = False

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def has_text(self) -> bool:
        return any(part.strip() for part in self._parts)

    def add(self, piece: str) -> None:
        if not piece:
            return
        room = self._max_chars - self._chars
        if len(piece) > room:
            self._parts.append(piece[:room])
            self._chars += room
            raise _TextFull(f"Extracted text reached {self._max_chars} characters")
        self._parts.append(piece)
        self._chars += len(piece)
        self.check_time()

    def check_time(self) -> None:
        if self._clock() > self._deadline:
            raise ProcessingLimitExceeded("Text extraction ran out of time")

    def count_elements(self, count: int) -> None:
        """`count` more XML elements parsed (and time checked)."""
        self._elements += count
        if self._elements > self._max_elements:
            raise ProcessingLimitExceeded(f"Parsed over {self._max_elements} XML elements")
        self.check_time()


class DocumentParser(Protocol):
    def extract(self, source: DocumentSource, limits: ParserLimits, text: TextCollector) -> None:
        """Adds the document's text to `text`, reading `source` within
        `limits`."""
        ...


def extract_text(parser: DocumentParser, source: DocumentSource, limits: ParserLimits) -> ExtractedText:
    """Runs `parser` on `source` within `limits`. A limit reached after
    some text was extracted ends extraction with that text, partial; before
    any, `ProcessingLimitExceeded` propagates. Any other exception is the
    parser's (or storage's): see `DocumentSource.storage_error`."""
    text = TextCollector(limits)
    stopped_by = None
    try:
        parser.extract(source, limits, text)
    except ProcessingLimitExceeded as exc:
        if not text.has_text():
            raise
        stopped_by = str(exc)
    return ExtractedText(text=text.text, is_partial=text.partial or stopped_by is not None, stopped_by=stopped_by)


# ---- text decoding ----


def decode_text(data: bytes, *, complete: bool = True) -> str:
    """Best-effort decoding of a plain-text file: UTF-8 (with or without a
    BOM) if valid, otherwise whatever encoding charset-normalizer detects
    (e.g. windows-1251 for older Russian files). `complete=False`: `data`
    is the file's beginning only, which may end in the middle of a
    character."""
    try:
        return codecs.getincrementaldecoder("utf-8-sig")().decode(data, final=complete)
    except UnicodeDecodeError:
        pass
    best = charset_normalizer.from_bytes(data).best()
    return str(best) if best is not None else data.decode("utf-8", errors="replace")


def _range_decoder(head: bytes) -> Callable[[bytes], str | None]:
    """Decodes ranges from anywhere in a text file whose beginning is
    `head`: in the encoding the head is in, dropping characters cut at a
    range's edges. None for encodings that can't be decoded from an
    arbitrary byte (UTF-16/32)."""
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        encoding = "utf-8"
    except UnicodeDecodeError:
        best = charset_normalizer.from_bytes(head).best()
        encoding = best.encoding if best is not None else "utf-8"
    if codecs.lookup(encoding).name.startswith(("utf-16", "utf-32")):
        return lambda data: None
    return lambda data: data.decode(encoding, errors="ignore")


# ---- HTML ----


class _HtmlTextExtractor(HTMLParser):
    _SKIPPED = {"script", "style", "template", "noscript"}
    _BLOCKS = {
        "p", "div", "br", "li", "tr", "section", "article", "header", "footer", "blockquote",
        "h1", "h2", "h3", "h4", "h5", "h6", "title", "pre", "table", "ul", "ol", "dt", "dd",
    }

    def __init__(self, text: TextCollector):
        super().__init__(convert_charrefs=True)
        self._text = text
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIPPED:
            self._skip_depth += 1
        elif tag in self._BLOCKS:
            self._text.add("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIPPED:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCKS:
            self._text.add("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._text.add(data)


# Fed to the HTML parser in pieces this size, so time and the text limit
# are checked as it goes.
_HTML_CHUNK_CHARS = 64 * 1024


def _html_text(html: str, text: TextCollector) -> None:
    extractor = _HtmlTextExtractor(text)
    for start in range(0, len(html), _HTML_CHUNK_CHARS):
        text.check_time()
        extractor.feed(html[start : start + _HTML_CHUNK_CHARS])
    extractor.close()


# ---- XML, streamed ----

# Decides which elements are blocks — paragraphs, headings... — whose text
# becomes one line: given the local names of an element's ancestors and its
# own, the string its text pieces are joined with, or None if it isn't one.
# The outermost block wins; text outside blocks is dropped.
BlockRule = Callable[[tuple[str, ...], str], str | None]

# Elements between checks of the element and time limits.
_XML_CHECK_EVERY = 1_000


def _local_name(tag: object) -> str:
    # ElementTree tags look like "{namespace}name"; comments/PIs aren't str.
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


@dataclass
class _Open:
    """An element being parsed: its last child that ended (whose tail
    comes next), if any."""

    element: object
    name: str
    last_child: object | None = None


def _xml_text(stream, rule: BlockRule | None, text: TextCollector, limits: ParserLimits) -> None:
    """Adds the text of the XML document `stream` (a binary file) to
    `text`, in document order: with `rule`, one line per block (see
    `BlockRule`); without, one line per non-blank piece of text.

    Streamed: each piece of text is taken as soon as the parser has it — an
    element's leading text when its first child starts, a child's tail when
    the next child starts or the parent ends — and each element is removed
    from its parent once its tail is taken. So what's kept in memory is the
    elements on the current path, however long the document."""
    stack: list[_Open] = []
    block_at: int | None = None  # index in `stack` of the block being read
    block_parts: list[str] = []
    block_chars = 0
    joiner = ""

    def take(piece: str | None, owner: int) -> None:
        """A piece of text belonging to `stack[owner]`."""
        nonlocal block_chars
        if not piece:
            return
        if block_at is not None and owner >= block_at:
            block_parts.append(piece)
            block_chars += len(piece)
            if block_chars > limits.max_text_chars:
                # One block over the whole text limit: it can't fit anyway.
                text.add("".join(block_parts))
        elif rule is None and piece.strip():
            text.add(piece.strip() + "\n")

    elements = 0
    for event, element in ElementTree.iterparse(stream, events=("start", "end")):
        if event == "start":
            elements += 1
            if elements % _XML_CHECK_EVERY == 0:
                text.count_elements(_XML_CHECK_EVERY)
            name = _local_name(element.tag)
            if stack:
                parent = stack[-1]
                previous = parent.last_child
                take(parent.element.text if previous is None else previous.tail, len(stack) - 1)
                if previous is not None:
                    parent.element.remove(previous)
                    parent.last_child = None
            if rule is not None and block_at is None:
                block_joiner = rule(tuple(frame.name for frame in stack), name)
                if block_joiner is not None:
                    block_at, block_parts, block_chars, joiner = len(stack), [], 0, block_joiner
            stack.append(_Open(element, name))
        else:
            current = stack.pop()
            depth = len(stack)
            previous = current.last_child
            take(current.element.text if previous is None else previous.tail, depth)
            if previous is not None:
                current.element.remove(previous)
            if block_at == depth:
                if joiner:
                    line = joiner.join(part.strip() for part in block_parts if part.strip())
                    if line:
                        text.add(line + "\n")
                else:
                    text.add("".join(block_parts) + "\n")
                block_at = None
            if stack:
                stack[-1].last_child = current.element


def _parse_xml(data: bytes, cut: bool, rule: BlockRule | None, text: TextCollector, limits: ParserLimits) -> None:
    """`_xml_text` over `data`, a file's beginning if `cut`: then the
    document ends early by design, and the text up to there is kept."""
    try:
        _xml_text(io.BytesIO(data), rule, text, limits)
    except ElementTree.ParseError as exc:
        if not cut:
            raise
        raise ProcessingLimitExceeded(f"Read only the first {len(data)} bytes") from exc


# ---- ZIP ----

_EOCD = struct.Struct("<4s4H2LH")
_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_EOCD = struct.Struct("<4sQ2H2L4Q")
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
# The largest the end-of-directory record can be (a maximal comment).
_EOCD_MAX_SIZE = _EOCD.size + 0xFFFF
# Generous for a directory entry (46 bytes plus its name, extra field and
# comment); a directory larger than this per declared entry is refused.
_DIRECTORY_BYTES_PER_ENTRY = 1024
# General-purpose flag bit 0: the member is encrypted.
_ENCRYPTED = 0x1
# Members up to this size may compress however well: small, highly
# repetitive XML parts are harmless.
_RATIO_GRACE_BYTES = 1024 * 1024


def _zip_directory(reader: RangeReader) -> tuple[int, int]:
    """(entries, directory size in bytes) from a ZIP's end-of-directory
    records, read before `zipfile` reads (and builds an object for every
    entry of) the directory itself."""
    tail_size = min(reader.size, _EOCD_MAX_SIZE)
    reader.seek(reader.size - tail_size)
    tail = reader.read(tail_size)
    at = tail.rfind(_EOCD_SIGNATURE)
    if at < 0 or len(tail) - at < _EOCD.size:
        raise UnparsableDocumentError("Not a ZIP archive")
    _, _, _, _, entries, directory_size, _, _ = _EOCD.unpack_from(tail, at)
    if (entries == 0xFFFF or directory_size == 0xFFFFFFFF) and at >= _ZIP64_LOCATOR.size:
        signature, _, zip64_at, _ = _ZIP64_LOCATOR.unpack_from(tail, at - _ZIP64_LOCATOR.size)
        if signature == _ZIP64_LOCATOR_SIGNATURE and zip64_at + _ZIP64_EOCD.size <= reader.size:
            reader.seek(zip64_at)
            record = _ZIP64_EOCD.unpack(reader.read(_ZIP64_EOCD.size))
            if record[0] == _ZIP64_EOCD_SIGNATURE:
                entries, directory_size = record[7], record[8]
    return entries, directory_size


class _Inflated(io.RawIOBase):
    """A member being decompressed, counted as it's read (whatever it
    declares) against the archive's shared `budget` of decompressed bytes
    and its own `limit`, which `limit_reason` explains."""

    def __init__(self, member, budget: list[int], limit: int, limit_reason: str):
        super().__init__()
        self._member = member
        self._budget = budget
        self._left = limit
        self._limit_reason = limit_reason

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self._member.read(len(buffer))
        if len(data) > self._budget[0]:
            raise ProcessingLimitExceeded("Decompressed data reached the archive limit")
        if len(data) > self._left:
            raise ProcessingLimitExceeded(self._limit_reason)
        self._budget[0] -= len(data)
        self._left -= len(data)
        buffer[: len(data)] = data
        return len(data)


class _Archive:
    """A ZIP document, opened within `limits`: members are read through
    `member`, all of them together inflating at most
    `max_uncompressed_bytes`."""

    def __init__(self, zip_file: zipfile.ZipFile, limits: ParserLimits):
        self.zip = zip_file
        self._limits = limits
        self._budget = [limits.max_uncompressed_bytes]

    def names(self) -> list[str]:
        return self.zip.namelist()

    @contextmanager
    def member(self, name: str) -> Iterator[io.BufferedReader]:
        """The member, as it's inflated: at most `max_member_bytes` of it,
        and (past `_RATIO_GRACE_BYTES`) `max_compression_ratio` times its
        compressed size, both set from its directory entry before anything
        is inflated. Reading past either ends extraction there, like the
        archive's budget: a document's text up to that point is still
        worth describing. (`zipfile` itself never inflates more than the
        entry declares.)"""
        info = self.zip.getinfo(name)
        if info.flag_bits & _ENCRYPTED:
            raise UnparsableDocumentError(f"Archive member {name!r} is encrypted")
        limits = self._limits
        ratio_limit = max(_RATIO_GRACE_BYTES, limits.max_compression_ratio * info.compress_size)
        if ratio_limit < limits.max_member_bytes:
            limit = ratio_limit
            reason = f"Archive member {name!r} inflates past {limits.max_compression_ratio}:1"
        else:
            limit = limits.max_member_bytes
            reason = f"Archive member {name!r} inflates past {limits.max_member_bytes} bytes"
        with self.zip.open(name) as member:
            yield io.BufferedReader(_Inflated(member, self._budget, limit, reason))

    def read(self, name: str) -> bytes:
        with self.member(name) as stream:
            return stream.read()


@contextmanager
def _open_archive(source: DocumentSource, limits: ParserLimits) -> Iterator[_Archive]:
    reader = source.open(max_bytes_read=limits.max_bytes_read)
    entries, directory_size = _zip_directory(reader)
    if entries > limits.max_archive_entries:
        raise ProcessingLimitExceeded(f"Archive has {entries} entries (limit {limits.max_archive_entries})")
    # `zipfile` reads the directory by its size, not its declared entries.
    if directory_size > max(entries, 1) * _DIRECTORY_BYTES_PER_ENTRY:
        raise ProcessingLimitExceeded(f"Archive directory is {directory_size} bytes for {entries} entries")
    reader.seek(0)
    with zipfile.ZipFile(reader) as zip_file:
        members = zip_file.infolist()
        if len(members) > limits.max_archive_entries:
            raise ProcessingLimitExceeded(
                f"Archive lists {len(members)} entries (limit {limits.max_archive_entries})"
            )
        for member in members:
            if not _is_contained(member.filename):
                raise UnparsableDocumentError(f"Archive member {member.filename!r} points outside the archive")
        yield _Archive(zip_file, limits)


_DRIVE = re.compile(r"[A-Za-z]:")


def _is_contained(name: str) -> bool:
    """A member name that stays inside the archive if extracted: relative,
    no `..` (zip-slip), no drive. Office suites and e-book tools never
    write anything else."""
    parts = re.split(r"[/\\]", name)
    return parts[0] != "" and not _DRIVE.match(parts[0]) and ".." not in parts


# ---- parsers ----


class PlainTextParser:
    """TXT, Markdown, CSV/TSV, JSON, YAML, logs: the text is the content.
    Up to `max_prefix_bytes`, read whole; larger, only the excerpts sent
    to the model are read (see `excerpt.excerpt_from_ranges`)."""

    def extract(self, source: DocumentSource, limits: ParserLimits, text: TextCollector) -> None:
        if source.size <= limits.max_prefix_bytes:
            text.add(decode_text(source.read(0, source.size)))
            return
        text.partial = True
        # Enough of the head to tell its encoding.
        decode = _range_decoder(source.read(0, 64 * 1024))
        text.add(excerpt_from_ranges(source.size, source.read, decode, limits.excerpt_chars))


class HtmlParser:
    def extract(self, source: DocumentSource, limits: ParserLimits, text: TextCollector) -> None:
        data, cut = source.prefix(limits.max_prefix_bytes)
        text.partial = cut
        _html_text(decode_text(data, complete=not cut), text)


class XmlParser:
    """Generic XML: its text content, without the markup."""

    def extract(self, source: DocumentSource, limits: ParserLimits, text: TextCollector) -> None:
        data, cut = source.prefix(limits.max_prefix_bytes)
        text.partial = cut
        _parse_xml(data, cut, None, text, limits)


def _pdf_pages(count: int, max_pages: int) -> list[int]:
    """Which of `count` pages to read: all if there are at most
    `max_pages`, else the first half of them, then the rest evenly spread
    over the remainder (in order)."""
    if count <= max_pages:
        return list(range(count))
    head = max_pages // 2
    rest = max_pages - head
    step = (count - head) / rest
    return list(range(head)) + [head + int(index * step + step / 2) for index in range(rest)]


class _PageTooComplex(ProcessingLimitExceeded):
    pass


class _PdfBudget:
    """What extracting one PDF's text may still cost, charged by pypdf
    itself through three hooks, which see every one of these however pypdf
    got there (font, form XObject, object stream, recovery...):

    - bytes pypdf reads from the file (`_CountedFile`): bounds what it
      holds of the file in memory — it keeps every object it has read;
    - bytes it decompresses (`decode_stream_data`), all streams together:
      bounds what it holds decompressed, as it keeps that too;
    - bytes of content streams it parses (`ContentStream`), per page, before
      it parses them: parsing is what costs CPU, in one call that can't be
      interrupted.

    Opening the document — its cross-reference data and page tree — may
    read and decompress only `max_pdf_open_bytes`: pypdf holds that as
    Python objects, at ~10x its size, so it's kept far below the rest. Then
    the rest of `max_bytes_read` and `max_uncompressed_bytes` apply.

    Sticky: once over a limit, every later charge fails too, because pypdf
    catches some errors (e.g. from a form XObject) and carries on without
    that part. `check` raises if a limit was reached, whether or not pypdf
    let it through.
    """

    def __init__(self, limits: ParserLimits, check_time: Callable[[], None]):
        self._limits = limits
        self._check_time = check_time
        self._opening = True
        # Charged by `_CountedFile` itself, as cheaply as possible: pypdf
        # reads a byte at a time.
        self.read_left = limits.max_pdf_open_bytes
        self._decompress_left = limits.max_pdf_open_bytes
        self._page_content_left = limits.max_pdf_page_content_bytes
        self._exceeded: ProcessingLimitExceeded | None = None
        self._page_exceeded: _PageTooComplex | None = None

    def opened(self) -> None:
        """The document is open and its pages listed: what's left of the
        whole document's budgets applies from here."""
        limits = self._limits
        self.read_left += limits.max_bytes_read - limits.max_pdf_open_bytes
        self._decompress_left += limits.max_uncompressed_bytes - limits.max_pdf_open_bytes
        self._opening = False

    def read_limit_reached(self) -> ProcessingLimitExceeded:
        self.read_left = -1
        self._exceeded = ProcessingLimitExceeded(self._over("Read", self._limits.max_bytes_read))
        return self._exceeded

    def decompressed(self, count: int) -> None:
        self.check()
        if count > self._decompress_left:
            self._exceeded = ProcessingLimitExceeded(self._over("Decompressed", self._limits.max_uncompressed_bytes))
            raise self._exceeded
        self._decompress_left -= count

    def _over(self, what: str, limit: int) -> str:
        if self._opening:
            return f"{what} over {self._limits.max_pdf_open_bytes} bytes opening the PDF (cross-references, page tree)"
        return f"{what} over {limit} bytes of the PDF"

    def parse(self, count: int) -> None:
        self.check()
        if count > self._page_content_left:
            self._page_exceeded = _PageTooComplex(
                f"Page content is over {self._limits.max_pdf_page_content_bytes} bytes"
            )
            raise self._page_exceeded
        self._page_content_left -= count

    def next_page(self) -> None:
        self._page_content_left = self._limits.max_pdf_page_content_bytes
        self._page_exceeded = None

    def check(self) -> None:
        if self._exceeded is not None:
            raise self._exceeded
        if self._page_exceeded is not None:
            raise self._page_exceeded

    def check_all(self) -> None:
        """`check`, and the time limit."""
        self.check()
        self._check_time()


# The budget of the extraction running in this thread, if any.
_pdf_budget: ContextVar[_PdfBudget | None] = ContextVar("pdf_budget", default=None)

# pypdf decodes every stream through `filters.decode_stream_data`, looked
# up at each call (`EncodedStreamObject.get_data`), and parses every content
# stream through `ContentStream._parse_content_stream`. Both are wrapped
# once, process-wide, to charge the running extraction's budget; outside
# one they behave as before. (Tests check the hooks see what they should,
# in case a pypdf upgrade moves them.)
_decode_stream_data = pypdf.filters.decode_stream_data
_parse_content_stream = ContentStream._parse_content_stream


def _counted_decode_stream_data(stream):
    data = _decode_stream_data(stream)
    if (budget := _pdf_budget.get()) is not None:
        budget.decompressed(len(data))
    return data


def _counted_parse_content_stream(self, stream):
    if (budget := _pdf_budget.get()) is not None:
        budget.parse(len(self._data or b""))
    return _parse_content_stream(self, stream)


pypdf.filters.decode_stream_data = _counted_decode_stream_data
ContentStream._parse_content_stream = _counted_parse_content_stream


# Reads of the file between checks of the time limit, while pypdf opens it
# (it can't be interrupted otherwise).
_READS_BETWEEN_CHECKS = 0x10000


class _CountedFile:
    """The downloaded PDF as pypdf reads it: every read charged to the
    budget before it's made (the whole rest of the file, for a read of all
    of it, which pypdf does to recover a damaged one), and time checked
    every `_READS_BETWEEN_CHECKS` reads. Just what pypdf needs of a file,
    and cheap per call: pypdf reads a byte at a time."""

    def __init__(self, file: IO[bytes], budget: _PdfBudget):
        self._file = file
        self._size = os.fstat(file.fileno()).st_size
        self._budget = budget
        self._reads = 0
        self.seek = file.seek
        self.tell = file.tell

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            size = max(0, self._size - self._file.tell())
        budget = self._budget
        if size > budget.read_left:
            raise budget.read_limit_reached()
        budget.read_left -= size
        self._reads += 1
        if not self._reads % _READS_BETWEEN_CHECKS:
            budget.check_all()
        return self._file.read(size)


# PDF operators between checks of the time limit (and the budget).
_PDF_CHECK_EVERY = 1_000


def _every(count: int, check: Callable[[], None]) -> Callable[..., None]:
    """A pypdf visitor that calls `check` every `count` operators — also
    those of form XObjects, which pypdf visits too."""
    operators = itertools.count(1)

    def visit(*_args) -> None:
        if next(operators) % count == 0:
            check()

    return visit


class PdfParser:
    """Needs random access to the whole file (see the module docstring):
    downloaded to local disk first, within `max_download_bytes`, then read
    within `_PdfBudget`."""

    def extract(self, source: DocumentSource, limits: ParserLimits, text: TextCollector) -> None:
        stream_limit = limits.max_pdf_stream_bytes
        budget = _PdfBudget(limits, text.check_time)
        token = _pdf_budget.set(budget)
        try:
            with pypdf.apply_configuration(
                maximum_declared_stream_length=limits.max_bytes_read,
                array_based_stream_maximum_output_length=stream_limit,
                jbig2_maximum_output_length=stream_limit,
                lzw_maximum_output_length=stream_limit,
                run_length_maximum_output_length=stream_limit,
                zlib_maximum_output_length=stream_limit,
                image_maximum_buffer_size=stream_limit,
                page_tree_maximum_entries=limits.max_pdf_page_count,
                # Never run an external decoder on an uploaded file.
                jbig2dec_binary=None,
            ):
                with source.downloaded(max_bytes=limits.max_download_bytes, check_time=text.check_time) as file:
                    self._extract(PdfReader(_CountedFile(file, budget)), budget, limits, text)
        except LimitReachedError as exc:
            raise ProcessingLimitExceeded(f"PDF limit reached: {exc}") from exc
        finally:
            _pdf_budget.reset(token)

    @staticmethod
    def _extract(reader: PdfReader, budget: _PdfBudget, limits: ParserLimits, text: TextCollector) -> None:
        if reader.is_encrypted and not reader.decrypt(""):
            # Openable only with a password we don't have.
            raise UnparsableDocumentError("PDF is password-protected")
        count = len(reader.pages)
        # pypdf may have caught a limit and made do without part of the file.
        budget.check()
        budget.opened()
        pages = _pdf_pages(count, limits.max_pdf_pages)
        text.partial = len(pages) < count

        too_complex = 0
        for index in pages:
            budget.next_page()
            budget.check_all()
            try:
                page = reader.pages[index]
                page_text = page.extract_text(visitor_operand_before=_every(_PDF_CHECK_EVERY, budget.check_all))
                budget.check()
            except _PageTooComplex:
                # Skipped, as if pypdf had let it through without that part.
                too_complex += 1
                text.partial = True
                continue
            text.add((page_text or "") + "\n\n")
        if too_complex and not text.has_text():
            raise ProcessingLimitExceeded(
                f"{too_complex} page(s) read had over {limits.max_pdf_page_content_bytes} bytes of content"
            )


class ZipXmlParser:
    """Zip-of-XML formats (OOXML: docx/xlsx/pptx; OpenDocument: odt/ods/odp):
    the text of each block element (paragraph, heading, shared string...) in
    the XML parts `select_members` picks, in the order it returns them. Only
    those parts are read (and inflated): never images or other media."""

    def __init__(self, select_members: Callable[[list[str]], list[str]], block_names: set[str]):
        self._select_members = select_members
        self._block_names = block_names

    def extract(self, source: DocumentSource, limits: ParserLimits, text: TextCollector) -> None:
        rule: BlockRule = lambda ancestors, name: "" if name in self._block_names else None  # noqa: E731
        with _open_archive(source, limits) as archive:
            members = self._select_members(archive.names())
            if not members:
                raise UnparsableDocumentError("Expected document parts are missing")
            for name in members:
                with archive.member(name) as stream:
                    _xml_text(stream, rule, text, limits)


def _members_named(*names: str) -> Callable[[list[str]], list[str]]:
    return lambda available: [name for name in names if name in available]


def _numbered_members(pattern: str) -> Callable[[list[str]], list[str]]:
    """E.g. ppt/slides/slide1.xml, slide2.xml, ... slide10.xml in slide
    order (numerically, not lexically)."""
    regex = re.compile(pattern)

    def select(available: list[str]) -> list[str]:
        numbered = [(int(match.group(1)), name) for name in available if (match := regex.fullmatch(name))]
        return [name for _number, name in sorted(numbered)]

    return select


class EpubParser:
    """EPUB: the XHTML content documents in reading (spine) order."""

    def extract(self, source: DocumentSource, limits: ParserLimits, text: TextCollector) -> None:
        with _open_archive(source, limits) as archive:
            chapters = self._spine_members(archive) or sorted(
                name for name in archive.names() if name.lower().endswith((".xhtml", ".html", ".htm"))
            )
            if not chapters:
                raise UnparsableDocumentError("EPUB has no content documents")
            for name in chapters:
                _html_text(decode_text(archive.read(name)), text)
                text.add("\n\n")

    @staticmethod
    def _spine_members(archive: _Archive) -> list[str]:
        """Content documents listed in the package's spine, or [] if the
        package metadata is missing or malformed (the caller falls back to
        every (X)HTML file in the archive)."""
        try:
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(el for el in container.iter() if _local_name(el.tag) == "rootfile")
            opf_path = rootfile.attrib["full-path"]
            package = ElementTree.fromstring(archive.read(opf_path))
        except (KeyError, StopIteration, ElementTree.ParseError):
            return []

        base = posixpath.dirname(opf_path)
        hrefs = {
            el.attrib.get("id"): el.attrib.get("href")
            for el in package.iter()
            if _local_name(el.tag) == "item"
        }
        available = set(archive.names())
        members = []
        for itemref in (el for el in package.iter() if _local_name(el.tag) == "itemref"):
            href = hrefs.get(itemref.attrib.get("idref"))
            if href:
                name = posixpath.normpath(posixpath.join(base, href))
                if name in available:
                    members.append(name)
        return members


_FB2_METADATA = {"book-title", "author", "annotation", "keywords", "genre"}
_FB2_BODY_BLOCKS = {"p", "v", "subtitle", "text-author"}


def _fb2_block(ancestors: tuple[str, ...], name: str) -> str | None:
    """FictionBook 2: title, authors, annotation and keywords from the
    metadata (their words joined by spaces), then the body text."""
    if ancestors and ancestors[-1] == "title-info" and name in _FB2_METADATA:
        return " "
    if "body" in ancestors and name in _FB2_BODY_BLOCKS:
        return ""
    return None


class Fb2Parser:
    """FictionBook 2 (see `_fb2_block`). The XML prolog declares its
    encoding (often windows-1251), which the parser honors."""

    def extract(self, source: DocumentSource, limits: ParserLimits, text: TextCollector) -> None:
        data, cut = source.prefix(limits.max_prefix_bytes)
        text.partial = cut
        _parse_xml(data, cut, _fb2_block, text, limits)


class Fb2ZipParser:
    """A zip holding a single .fb2 book."""

    def extract(self, source: DocumentSource, limits: ParserLimits, text: TextCollector) -> None:
        with _open_archive(source, limits) as archive:
            books = [name for name in archive.names() if name.lower().endswith(".fb2")]
            if not books:
                raise UnparsableDocumentError("Archive contains no .fb2 book")
            with archive.member(books[0]) as stream:
                _xml_text(stream, _fb2_block, text, limits)


class RtfParser:
    _CODEPAGE = re.compile(rb"\\ansicpg(\d+)")

    def extract(self, source: DocumentSource, limits: ParserLimits, text: TextCollector) -> None:
        data, cut = source.prefix(limits.max_prefix_bytes)
        text.partial = cut
        # RTF is 7-bit text; non-ASCII characters are escapes decoded using
        # the document's declared code page (default Windows-1252).
        match = self._CODEPAGE.search(data[:4096])
        encoding = f"cp{match.group(1).decode()}" if match else "cp1252"
        try:
            "".encode(encoding)
        except LookupError:
            encoding = "cp1252"
        text.add(rtf_to_text(data.decode("latin-1"), encoding=encoding, errors="replace"))


_OOXML_PARAGRAPHS = {"p"}  # w:p (Word), a:p (PowerPoint drawing text)
_ODF_BLOCKS = {"p", "h"}  # text:p, text:h

_PLAIN_TEXT = PlainTextParser()

_PARSERS: dict[str, DocumentParser] = {
    "application/pdf": PdfParser(),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ZipXmlParser(
        _members_named("word/document.xml"), _OOXML_PARAGRAPHS
    ),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ZipXmlParser(
        _numbered_members(r"ppt/slides/slide(\d+)\.xml"), _OOXML_PARAGRAPHS
    ),
    # Cell text lives in the shared-strings table (numbers aren't useful here).
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ZipXmlParser(
        _members_named("xl/sharedStrings.xml"), {"si"}
    ),
    "application/vnd.oasis.opendocument.text": ZipXmlParser(_members_named("content.xml"), _ODF_BLOCKS),
    "application/vnd.oasis.opendocument.spreadsheet": ZipXmlParser(_members_named("content.xml"), _ODF_BLOCKS),
    "application/vnd.oasis.opendocument.presentation": ZipXmlParser(_members_named("content.xml"), _ODF_BLOCKS),
    "application/rtf": RtfParser(),
    "application/epub+zip": EpubParser(),
    "application/x-fictionbook+xml": Fb2Parser(),
    "application/x-zip-compressed-fb2": Fb2ZipParser(),
    "text/html": HtmlParser(),
    "application/xml": XmlParser(),
    "text/plain": _PLAIN_TEXT,
    "text/markdown": _PLAIN_TEXT,
    "text/csv": _PLAIN_TEXT,
    "text/tab-separated-values": _PLAIN_TEXT,
    "application/json": _PLAIN_TEXT,
    "application/yaml": _PLAIN_TEXT,
}


def parser_for(content_type: str) -> DocumentParser | None:
    """The parser for a stored content type (parameters like `charset` are
    ignored), or None if the format isn't supported."""
    return _PARSERS.get(content_type.split(";", 1)[0].strip().lower())


def supported_content_types() -> set[str]:
    return set(_PARSERS)


_SPACES = re.compile(r"[ \t\u00a0\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n\s*\n+")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


def normalize_text(text: str) -> str:
    """Tidies extracted text so the character budget isn't spent on layout:
    unified newlines, no control characters, single spaces, at most one
    blank line in a row."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()
