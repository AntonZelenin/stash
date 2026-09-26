"""Uploaded files: which formats Stash recognizes, and how an upload is
classified.

Any file can be stored as a `file` item. A *recognized* format gets its real
MIME type (and can be opened inline if it's safe to), and is marked
`analyzable` if Stash will extract its text once document analysis exists.
Everything else — unknown extensions, and files whose content doesn't match
their extension — is stored as a generic binary file: always downloaded,
never displayed inline, never analyzed.

Each recognized format also has a `ContentKind` (document, book, video,
audio, other), which the Files and Media filters group files by. It's never
stored: it follows from the stored content type (`kind_of`), so this module
is the only place that maps types to kinds.

A format is recognized by its extension *and* a cheap check of its content
(leading "magic" bytes, or for text formats, the absence of NUL bytes), so a
file can't get a type — or inline display — just by being renamed. Only the
first `SNIFF_BYTES` of the content are ever looked at: files are uploaded
straight to storage, and the API reads back just that much to classify them.
"""

import codecs
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

GENERIC_CONTENT_TYPE = "application/octet-stream"

# Formats a browser may render inline without risk to the storage origin.
# HTML, SVG and XML are deliberately absent: rendered, they can run scripts.
_INLINE_CONTENT_TYPES = {"application/pdf", "text/plain", "application/json"}

_MAX_FILENAME_LENGTH = 255
# How much of a file's content classification needs (magic bytes, NUL bytes
# in text, UTF-8 validity): what the API reads back from storage.
SNIFF_BYTES = 8192


def _starts_with(signature: bytes) -> Callable[[bytes], bool]:
    return lambda data: data.startswith(signature)


_is_pdf = _starts_with(b"%PDF-")
# ZIP container: OOXML (docx/xlsx/pptx), ODF, EPUB, Apple iWork, fb2.zip.
_is_zip = _starts_with(b"PK\x03\x04")
# OLE compound file: legacy Office (doc/xls/ppt).
_is_ole = _starts_with(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
_is_rtf = _starts_with(b"{\\rtf")
_is_djvu = _starts_with(b"AT&TFORM")


# ISO base media (MP4, M4A, MOV, 3GP): a size, then the first box's type.
# QuickTime files may start with another top-level box than `ftyp`.
_ISO_MEDIA_BOXES = {b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"}
# EBML: Matroska and WebM.
_is_ebml = _starts_with(b"\x1a\x45\xdf\xa3")
_is_ogg = _starts_with(b"OggS")
# ASF: Windows Media audio and video.
_is_asf = _starts_with(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11")
# ID3v2 tag, which MP3, AAC and FLAC files may start with.
_has_id3 = _starts_with(b"ID3")


def _is_iso_media(data: bytes) -> bool:
    return data[4:8] in _ISO_MEDIA_BOXES


def _is_riff(form: bytes) -> Callable[[bytes], bool]:
    # RIFF container (WAV, AVI): "RIFF", a size, then the form type.
    return lambda data: data.startswith(b"RIFF") and data[8:12] == form


def _is_mpeg_video(data: bytes) -> bool:
    # MPEG program stream pack header, or a sequence header.
    return data[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")


def _is_mp3(data: bytes) -> bool:
    # Tagged, or starting on an MPEG audio frame sync (11 set bits).
    return _has_id3(data) or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)


def _is_aac(data: bytes) -> bool:
    # Tagged, or starting on an ADTS frame sync (12 set bits, layer 0).
    return _has_id3(data) or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0)


def _is_flac(data: bytes) -> bool:
    return data.startswith(b"fLaC") or _has_id3(data)


def _is_mobi(data: bytes) -> bool:
    # MOBI/AZW/AZW3 are Palm database files with this type/creator at 60.
    return data[60:68] == b"BOOKMOBI"


def _is_text(data: bytes) -> bool:
    return b"\x00" not in data[:SNIFF_BYTES]


def _is_fb2(data: bytes) -> bool:
    # XML; often not UTF-8 (e.g. windows-1251), which the XML prolog declares.
    return _is_text(data) and b"<FictionBook" in data[:SNIFF_BYTES]


class ContentKind(str, Enum):
    """What an image or file item holds, as the Media (image, video,
    audio) and Files (document, book, other) filters group them. Images are
    always `image`: their item type says so. A file's kind follows from its
    stored content type (`kind_of`). PDFs are documents, since nothing
    reliable tells a PDF book apart; `other` is everything that isn't one
    of the rest (archives, unrecognized and generic files)."""

    image = "image"
    video = "video"
    audio = "audio"
    document = "document"
    book = "book"
    other = "other"


@dataclass(frozen=True)
class FileFormat:
    content_type: str
    matches: Callable[[bytes], bool]
    kind: ContentKind = ContentKind.document
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
    ".epub": FileFormat("application/epub+zip", _is_zip, ContentKind.book, analyzable=True),
    ".fb2": FileFormat("application/x-fictionbook+xml", _is_fb2, ContentKind.book, analyzable=True),
    ".fb2.zip": FileFormat("application/x-zip-compressed-fb2", _is_zip, ContentKind.book, analyzable=True),
    ".mobi": FileFormat("application/x-mobipocket-ebook", _is_mobi, ContentKind.book),
    ".azw": FileFormat("application/vnd.amazon.ebook", _is_mobi, ContentKind.book),
    ".azw3": FileFormat("application/vnd.amazon.ebook", _is_mobi, ContentKind.book),
    ".djvu": FileFormat("image/vnd.djvu", _is_djvu, ContentKind.book),
    ".djv": FileFormat("image/vnd.djvu", _is_djvu, ContentKind.book),
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
    # Video
    ".mp4": FileFormat("video/mp4", _is_iso_media, ContentKind.video),
    ".m4v": FileFormat("video/x-m4v", _is_iso_media, ContentKind.video),
    ".mov": FileFormat("video/quicktime", _is_iso_media, ContentKind.video),
    ".3gp": FileFormat("video/3gpp", _is_iso_media, ContentKind.video),
    ".webm": FileFormat("video/webm", _is_ebml, ContentKind.video),
    ".mkv": FileFormat("video/x-matroska", _is_ebml, ContentKind.video),
    ".avi": FileFormat("video/x-msvideo", _is_riff(b"AVI "), ContentKind.video),
    ".mpg": FileFormat("video/mpeg", _is_mpeg_video, ContentKind.video),
    ".mpeg": FileFormat("video/mpeg", _is_mpeg_video, ContentKind.video),
    ".ogv": FileFormat("video/ogg", _is_ogg, ContentKind.video),
    ".wmv": FileFormat("video/x-ms-wmv", _is_asf, ContentKind.video),
    # Audio
    ".mp3": FileFormat("audio/mpeg", _is_mp3, ContentKind.audio),
    ".m4a": FileFormat("audio/mp4", _is_iso_media, ContentKind.audio),
    ".aac": FileFormat("audio/aac", _is_aac, ContentKind.audio),
    ".wav": FileFormat("audio/wav", _is_riff(b"WAVE"), ContentKind.audio),
    ".flac": FileFormat("audio/flac", _is_flac, ContentKind.audio),
    ".ogg": FileFormat("audio/ogg", _is_ogg, ContentKind.audio),
    ".oga": FileFormat("audio/ogg", _is_ogg, ContentKind.audio),
    ".opus": FileFormat("audio/ogg", _is_ogg, ContentKind.audio),
    ".mka": FileFormat("audio/x-matroska", _is_ebml, ContentKind.audio),
    ".wma": FileFormat("audio/x-ms-wma", _is_asf, ContentKind.audio),
    # Archives
    ".zip": FileFormat("application/zip", _is_zip, ContentKind.other),
}


def _with_charset(content_type: str) -> str:
    return f"{content_type}; charset=utf-8"


def _stored_content_types(file_format: FileFormat) -> set[str]:
    """Every `content_type` `classify` can give a file of this format."""
    if file_format.is_text:
        return {file_format.content_type, _with_charset(file_format.content_type)}
    return {file_format.content_type}


# The stored content types of each file kind but `other`, which is
# whatever isn't in any of these (and `image`, which files never are).
# Filtering matches these exactly, so it works in any database.
FILE_KIND_CONTENT_TYPES: dict[ContentKind, frozenset[str]] = {
    kind: frozenset(
        content_type
        for file_format in FORMATS.values()
        if file_format.kind == kind
        for content_type in _stored_content_types(file_format)
    )
    for kind in (ContentKind.document, ContentKind.book, ContentKind.video, ContentKind.audio)
}


def kind_of(content_type: str) -> ContentKind:
    """A file's kind, from its stored (validated) content type."""
    for kind, content_types in FILE_KIND_CONTENT_TYPES.items():
        if content_type in content_types:
            return kind
    return ContentKind.other


@dataclass(frozen=True)
class ClassifiedFile:
    # Recognized format's extension (for the storage key), or "" for a
    # generic file.
    extension: str
    content_type: str
    recognized: bool
    analyzable: bool


@dataclass(frozen=True)
class ExpectedFormat:
    # Recognized extension (for the storage key), or "" for a generic file.
    extension: str
    # Without a charset: that depends on the content.
    content_type: str


def expected_format(filename: str) -> ExpectedFormat:
    """What an upload named `filename` (already cleaned) should be, from
    its extension alone, before its content exists: picks the storage key's
    extension and the type the object is stored with. `classify` still
    decides once the content is uploaded, and may fall back to generic."""
    for extension in _candidate_extensions(filename):
        file_format = FORMATS.get(extension)
        if file_format is not None:
            return ExpectedFormat(extension=extension, content_type=file_format.content_type)
    return ExpectedFormat(extension="", content_type=GENERIC_CONTENT_TYPE)


def classify(filename: str, data: bytes) -> ClassifiedFile:
    """Classifies an upload by its (already cleaned) filename and content;
    `data` may be just the file's first `SNIFF_BYTES`. Never rejects: an
    unrecognized or mismatched file comes back generic."""
    for extension in _candidate_extensions(filename):
        file_format = FORMATS.get(extension)
        if file_format is None:
            continue
        if not file_format.matches(data):
            # Right extension, wrong content: don't trust either.
            break
        content_type = file_format.content_type
        if file_format.is_text and _is_utf8(data):
            content_type = _with_charset(content_type)
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
    """Whether the (leading part of the) content is valid UTF-8. Judged
    from the first `SNIFF_BYTES` only, and a character cut off at the end of
    that sample doesn't count against it."""
    try:
        codecs.getincrementaldecoder("utf-8")().decode(data[:SNIFF_BYTES], final=False)
    except UnicodeDecodeError:
        return False
    return True
