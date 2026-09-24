"""Uploaded files: which formats Stash recognizes, and how an upload is
classified.

Any file can be stored as a `file` item. A *recognized* format gets its real
MIME type (and can be opened inline if it's safe to), and is marked
`analyzable` if Stash will extract its text once document analysis exists.
Everything else — unknown extensions, and files whose content doesn't match
their extension — is stored as a generic binary file: always downloaded,
never displayed inline, never analyzed.

A format is recognized by its extension *and* a cheap check of its content
(leading "magic" bytes, or for text formats, the absence of NUL bytes), so a
file can't get a type — or inline display — just by being renamed.
"""

from collections.abc import Callable
from dataclasses import dataclass

GENERIC_CONTENT_TYPE = "application/octet-stream"

# Formats a browser may render inline without risk to the storage origin.
# HTML, SVG and XML are deliberately absent: rendered, they can run scripts.
_INLINE_CONTENT_TYPES = {"application/pdf", "text/plain", "application/json"}

_MAX_FILENAME_LENGTH = 255
# How much of a text file is checked for NUL bytes (binary content).
_TEXT_SNIFF_BYTES = 8192


def _starts_with(signature: bytes) -> Callable[[bytes], bool]:
    return lambda data: data.startswith(signature)


_is_pdf = _starts_with(b"%PDF-")
# ZIP container: OOXML (docx/xlsx/pptx), ODF, EPUB, Apple iWork, fb2.zip.
_is_zip = _starts_with(b"PK\x03\x04")
# OLE compound file: legacy Office (doc/xls/ppt).
_is_ole = _starts_with(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
_is_rtf = _starts_with(b"{\\rtf")
_is_djvu = _starts_with(b"AT&TFORM")


def _is_mobi(data: bytes) -> bool:
    # MOBI/AZW/AZW3 are Palm database files with this type/creator at 60.
    return data[60:68] == b"BOOKMOBI"


def _is_text(data: bytes) -> bool:
    return b"\x00" not in data[:_TEXT_SNIFF_BYTES]


def _is_fb2(data: bytes) -> bool:
    # XML; often not UTF-8 (e.g. windows-1251), which the XML prolog declares.
    return _is_text(data) and b"<FictionBook" in data[:_TEXT_SNIFF_BYTES]


@dataclass(frozen=True)
class FileFormat:
    content_type: str
    matches: Callable[[bytes], bool]
    # Plain text: gets `; charset=utf-8` when the content is valid UTF-8,
    # so browsers decode it right.
    is_text: bool = False
    # Stash will extract this format's text once document analysis exists.
    analyzable: bool = False


# Recognized formats, by lowercased extension (compound ones like ".fb2.zip"
# included; the longest matching extension wins).
FORMATS: dict[str, FileFormat] = {
    # Office and other documents
    ".pdf": FileFormat("application/pdf", _is_pdf, analyzable=True),
    ".docx": FileFormat(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", _is_zip, analyzable=True
    ),
    ".xlsx": FileFormat(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _is_zip, analyzable=True
    ),
    ".pptx": FileFormat(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", _is_zip, analyzable=True
    ),
    ".odt": FileFormat("application/vnd.oasis.opendocument.text", _is_zip, analyzable=True),
    ".ods": FileFormat("application/vnd.oasis.opendocument.spreadsheet", _is_zip, analyzable=True),
    ".odp": FileFormat("application/vnd.oasis.opendocument.presentation", _is_zip, analyzable=True),
    ".odg": FileFormat("application/vnd.oasis.opendocument.graphics", _is_zip),
    ".rtf": FileFormat("application/rtf", _is_rtf, analyzable=True),
    ".doc": FileFormat("application/msword", _is_ole),
    ".xls": FileFormat("application/vnd.ms-excel", _is_ole),
    ".ppt": FileFormat("application/vnd.ms-powerpoint", _is_ole),
    ".pages": FileFormat("application/vnd.apple.pages", _is_zip),
    ".numbers": FileFormat("application/vnd.apple.numbers", _is_zip),
    ".key": FileFormat("application/vnd.apple.keynote", _is_zip),
    # Books
    ".epub": FileFormat("application/epub+zip", _is_zip, analyzable=True),
    ".fb2": FileFormat("application/x-fictionbook+xml", _is_fb2, analyzable=True),
    ".fb2.zip": FileFormat("application/x-zip-compressed-fb2", _is_zip, analyzable=True),
    ".mobi": FileFormat("application/x-mobipocket-ebook", _is_mobi),
    ".azw": FileFormat("application/vnd.amazon.ebook", _is_mobi),
    ".azw3": FileFormat("application/vnd.amazon.ebook", _is_mobi),
    ".djvu": FileFormat("image/vnd.djvu", _is_djvu),
    ".djv": FileFormat("image/vnd.djvu", _is_djvu),
    # Text and data
    ".txt": FileFormat("text/plain", _is_text, is_text=True, analyzable=True),
    ".log": FileFormat("text/plain", _is_text, is_text=True, analyzable=True),
    ".md": FileFormat("text/markdown", _is_text, is_text=True, analyzable=True),
    ".csv": FileFormat("text/csv", _is_text, is_text=True, analyzable=True),
    ".tsv": FileFormat("text/tab-separated-values", _is_text, is_text=True, analyzable=True),
    ".json": FileFormat("application/json", _is_text, is_text=True, analyzable=True),
    ".yaml": FileFormat("application/yaml", _is_text, is_text=True, analyzable=True),
    ".yml": FileFormat("application/yaml", _is_text, is_text=True, analyzable=True),
    ".xml": FileFormat("application/xml", _is_text, is_text=True, analyzable=True),
    ".html": FileFormat("text/html", _is_text, is_text=True, analyzable=True),
    ".htm": FileFormat("text/html", _is_text, is_text=True, analyzable=True),
    # Archives
    ".zip": FileFormat("application/zip", _is_zip),
}


@dataclass(frozen=True)
class ClassifiedFile:
    # Recognized format's extension (for the storage key), or "" for a
    # generic file.
    extension: str
    content_type: str
    recognized: bool
    analyzable: bool


def classify(filename: str, data: bytes) -> ClassifiedFile:
    """Classifies an upload by its (already cleaned) filename and content.
    Never rejects: an unrecognized or mismatched file comes back generic."""
    for extension in _candidate_extensions(filename):
        file_format = FORMATS.get(extension)
        if file_format is None:
            continue
        if not file_format.matches(data):
            # Right extension, wrong content: don't trust either.
            break
        content_type = file_format.content_type
        if file_format.is_text and _is_utf8(data):
            content_type = f"{content_type}; charset=utf-8"
        return ClassifiedFile(
            extension=extension,
            content_type=content_type,
            recognized=True,
            analyzable=file_format.analyzable,
        )
    return ClassifiedFile(extension="", content_type=GENERIC_CONTENT_TYPE, recognized=False, analyzable=False)


def is_inline(content_type: str) -> bool:
    """Whether a stored file may be displayed in the browser rather than
    downloaded."""
    return content_type.split(";", 1)[0].strip() in _INLINE_CONTENT_TYPES


def clean_filename(filename: str | None) -> str:
    """The client's filename minus any path (some browsers send one), capped
    in length but keeping the extension. Display-only: storage keys never
    use it."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if len(name) > _MAX_FILENAME_LENGTH:
        stem, dot, extension = name.rpartition(".")
        keep = _MAX_FILENAME_LENGTH - len(dot + extension)
        name = (stem[:keep] + dot + extension) if dot and keep > 0 else name[:_MAX_FILENAME_LENGTH]
    return name or "file"


def _candidate_extensions(filename: str) -> list[str]:
    """Longest first: "Book.fb2.zip" -> [".fb2.zip", ".zip"]."""
    parts = filename.lower().split(".")[1:]
    return ["." + ".".join(parts[i:]) for i in range(len(parts))] if parts else []


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
