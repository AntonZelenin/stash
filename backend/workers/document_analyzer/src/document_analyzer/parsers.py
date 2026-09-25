"""Plain-text extraction from uploaded documents.

Each supported format has a `DocumentParser`, looked up by the content type
the API stored for the file (see `app.items.files` on the API side, whose
`analyzable` formats are exactly the ones registered here). To support a new
format, implement `DocumentParser` and add it to `_PARSERS`.

Parsers are synchronous and CPU-bound — call them off the event loop. They
raise on input they can't parse (corrupt, encrypted, truncated...); parsing
is deterministic, so callers should treat that as permanent.
"""

import io
import posixpath
import re
import zipfile
from collections.abc import Callable, Iterable
from html.parser import HTMLParser
from typing import Protocol

import charset_normalizer
from defusedxml import ElementTree
from pypdf import PdfReader
from striprtf.striprtf import rtf_to_text

# A zip member whose declared uncompressed size exceeds this is refused
# rather than inflated (zip-bomb guard); real document parts are far smaller.
_MAX_ZIP_MEMBER_BYTES = 200 * 1024 * 1024


class UnparsableDocumentError(Exception):
    """The document's structure is invalid or unsupported (as opposed to a
    low-level library error, which callers treat the same way)."""


class DocumentParser(Protocol):
    def extract_text(self, data: bytes) -> str: ...


# ---- helpers ----


def _local_name(tag: object) -> str:
    # ElementTree tags look like "{namespace}name"; comments/PIs aren't str.
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _block_texts(root, block_names: set[str]) -> Iterable[str]:
    """Text of each outermost element named in `block_names` (paragraphs,
    headings...), in document order. Descending stops at a block, so nested
    blocks aren't emitted twice."""
    if _local_name(root.tag) in block_names:
        yield "".join(root.itertext())
        return
    for child in root:
        yield from _block_texts(child, block_names)


def _read_zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > _MAX_ZIP_MEMBER_BYTES:
        raise UnparsableDocumentError(f"Archive member {name!r} is too large to extract")
    return archive.read(info)


def decode_text(data: bytes) -> str:
    """Best-effort decoding of a plain-text file: UTF-8 (with or without a
    BOM) if valid, otherwise whatever encoding charset-normalizer detects
    (e.g. windows-1251 for older Russian files)."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    best = charset_normalizer.from_bytes(data).best()
    return str(best) if best is not None else data.decode("utf-8", errors="replace")


class _HtmlTextExtractor(HTMLParser):
    _SKIPPED = {"script", "style", "template", "noscript"}
    _BLOCKS = {
        "p", "div", "br", "li", "tr", "section", "article", "header", "footer", "blockquote",
        "h1", "h2", "h3", "h4", "h5", "h6", "title", "pre", "table", "ul", "ol", "dt", "dd",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIPPED:
            self._skip_depth += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIPPED:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    extractor = _HtmlTextExtractor()
    extractor.feed(html)
    extractor.close()
    return "".join(extractor.parts)


# ---- parsers ----


class PlainTextParser:
    """TXT, Markdown, CSV/TSV, JSON, YAML, logs: the text is the content."""

    def extract_text(self, data: bytes) -> str:
        return decode_text(data)


class HtmlParser:
    def extract_text(self, data: bytes) -> str:
        return html_to_text(decode_text(data))


class XmlParser:
    """Generic XML: its text content, without the markup."""

    def extract_text(self, data: bytes) -> str:
        root = ElementTree.fromstring(data)
        return "\n".join(text.strip() for text in root.itertext() if text.strip())


class PdfParser:
    def extract_text(self, data: bytes) -> str:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            # Openable only with a password we don't have.
            raise UnparsableDocumentError("PDF is password-protected")
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)


class ZipXmlParser:
    """Zip-of-XML formats (OOXML: docx/xlsx/pptx; OpenDocument: odt/ods/odp):
    the text of each block element (paragraph, heading, shared string...) in
    the XML parts `select_members` picks, in the order it returns them."""

    def __init__(self, select_members: Callable[[list[str]], list[str]], block_names: set[str]):
        self._select_members = select_members
        self._block_names = block_names

    def extract_text(self, data: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = self._select_members(archive.namelist())
            if not members:
                raise UnparsableDocumentError("Expected document parts are missing")
            paragraphs = []
            for name in members:
                root = ElementTree.fromstring(_read_zip_member(archive, name))
                paragraphs.extend(_block_texts(root, self._block_names))
        return "\n".join(paragraphs)


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

    def extract_text(self, data: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            chapters = self._spine_members(archive) or sorted(
                name for name in archive.namelist() if name.lower().endswith((".xhtml", ".html", ".htm"))
            )
            if not chapters:
                raise UnparsableDocumentError("EPUB has no content documents")
            return "\n\n".join(
                html_to_text(decode_text(_read_zip_member(archive, name))) for name in chapters
            )

    @staticmethod
    def _spine_members(archive: zipfile.ZipFile) -> list[str]:
        """Content documents listed in the package's spine, or [] if the
        package metadata is missing or malformed (the caller falls back to
        every (X)HTML file in the archive)."""
        try:
            container = ElementTree.fromstring(_read_zip_member(archive, "META-INF/container.xml"))
            rootfile = next(el for el in container.iter() if _local_name(el.tag) == "rootfile")
            opf_path = rootfile.attrib["full-path"]
            package = ElementTree.fromstring(_read_zip_member(archive, opf_path))
        except (KeyError, StopIteration, ElementTree.ParseError):
            return []

        base = posixpath.dirname(opf_path)
        hrefs = {
            el.attrib.get("id"): el.attrib.get("href")
            for el in package.iter()
            if _local_name(el.tag) == "item"
        }
        available = set(archive.namelist())
        members = []
        for itemref in (el for el in package.iter() if _local_name(el.tag) == "itemref"):
            href = hrefs.get(itemref.attrib.get("idref"))
            if href:
                name = posixpath.normpath(posixpath.join(base, href))
                if name in available:
                    members.append(name)
        return members


class Fb2Parser:
    """FictionBook 2: title, authors, annotation and keywords from the
    metadata, then the body text. The XML prolog declares its encoding
    (often windows-1251), which the parser honors."""

    _BODY_BLOCKS = {"p", "v", "subtitle", "text-author"}

    def extract_text(self, data: bytes) -> str:
        root = ElementTree.fromstring(data)
        parts: list[str] = []

        title_info = next((el for el in root.iter() if _local_name(el.tag) == "title-info"), None)
        if title_info is not None:
            for element in title_info:
                name = _local_name(element.tag)
                if name in {"book-title", "author", "annotation", "keywords", "genre"}:
                    text = " ".join(t.strip() for t in element.itertext() if t.strip())
                    if text:
                        parts.append(text)

        for body in (el for el in root if _local_name(el.tag) == "body"):
            parts.extend(_block_texts(body, self._BODY_BLOCKS))
        return "\n".join(parts)


class Fb2ZipParser:
    """A zip holding a single .fb2 book."""

    def extract_text(self, data: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            books = [name for name in archive.namelist() if name.lower().endswith(".fb2")]
            if not books:
                raise UnparsableDocumentError("Archive contains no .fb2 book")
            return Fb2Parser().extract_text(_read_zip_member(archive, books[0]))


class RtfParser:
    _CODEPAGE = re.compile(rb"\\ansicpg(\d+)")

    def extract_text(self, data: bytes) -> str:
        # RTF is 7-bit text; non-ASCII characters are escapes decoded using
        # the document's declared code page (default Windows-1252).
        match = self._CODEPAGE.search(data[:4096])
        encoding = f"cp{match.group(1).decode()}" if match else "cp1252"
        try:
            "".encode(encoding)
        except LookupError:
            encoding = "cp1252"
        return rtf_to_text(data.decode("latin-1"), encoding=encoding, errors="replace")


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
