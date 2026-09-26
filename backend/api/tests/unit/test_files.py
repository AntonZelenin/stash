import zipfile
from io import BytesIO

import pytest

from app.items.files import (
    FILE_KIND_CONTENT_TYPES,
    FORMATS,
    GENERIC_CONTENT_TYPE,
    SNIFF_BYTES,
    ContentKind,
    classify,
    clean_filename,
    expected_format,
    is_inline,
    kind_of,
)

_ZIP = b"PK\x03\x04" + b"\x00" * 64
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
_MOBI = b"\x00" * 60 + b"BOOKMOBI" + b"\x00" * 32
_FB2 = (
    '<?xml version="1.0" encoding="windows-1251"?>\n<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
    "<body><p>Глава первая</p></body></FictionBook>"
).encode("cp1251")
_MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32
_EBML = b"\x1a\x45\xdf\xa3" + b"\x00" * 32
_OGG = b"OggS" + b"\x00" * 32


def _real_epub() -> bytes:
    """An EPUB as produced by real tools: a zip whose first entry is the
    uncompressed `mimetype` file."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", "<container/>")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("filename", "data", "content_type", "analyzable"),
    [
        ("Report.PDF", b"%PDF-1.7 ...", "application/pdf", True),
        ("book.epub", _real_epub(), "application/epub+zip", True),
        ("Война и мир.fb2", _FB2, "application/x-fictionbook+xml", True),
        ("book.fb2.zip", _ZIP, "application/x-zip-compressed-fb2", True),
        ("book.mobi", _MOBI, "application/x-mobipocket-ebook", False),
        ("book.azw3", _MOBI, "application/vnd.amazon.ebook", False),
        ("scan.djvu", b"AT&TFORM\x00\x00", "image/vnd.djvu", False),
        ("notes.docx", _ZIP, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", True),
        ("old.doc", _OLE, "application/msword", False),
        ("deck.key", _ZIP, "application/vnd.apple.keynote", False),
        ("letter.rtf", b"{\\rtf1\\ansi hi}", "application/rtf", True),
        ("todo.txt", "купить молоко".encode(), "text/plain; charset=utf-8", True),
        ("data.json", b'{"a": 1}', "application/json; charset=utf-8", True),
        ("config.YML", b"a: 1\n", "application/yaml; charset=utf-8", True),
        ("page.html", b"<html></html>", "text/html; charset=utf-8", True),
        ("stuff.zip", _ZIP, "application/zip", False),
    ],
)
def test_recognized_formats(filename, data, content_type, analyzable):
    classified = classify(filename, data)

    assert classified.recognized
    assert classified.content_type == content_type
    assert classified.analyzable is analyzable
    assert filename.lower().endswith(classified.extension)


def test_text_that_is_not_utf8_is_recognized_without_charset():
    """E.g. an old windows-1251 .txt: still text, the browser guesses the
    encoding."""
    classified = classify("old.txt", "привет".encode("cp1251"))

    assert classified.recognized
    assert classified.content_type == "text/plain"


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("program.exe", b"MZ\x90\x00"),  # unknown extension
        ("Makefile", b"all:\n"),  # no extension
        ("photo.heic", b"\x00\x00\x00\x18ftypheic"),
        ("fake.pdf", b"<html>not a pdf</html>"),  # content doesn't match
        ("fake.docx", b"%PDF-1.7"),
        ("binary.txt", b"abc\x00\x01"),  # "text" with NUL bytes
        ("not-a-book.fb2", b"<?xml version='1.0'?><html/>"),
        ("fake.fb2.zip", b"%PDF-1.7"),  # doesn't fall back to a looser match
    ],
)
def test_everything_else_is_stored_as_a_generic_file(filename, data):
    classified = classify(filename, data)

    assert not classified.recognized
    assert not classified.analyzable
    assert classified.content_type == GENERIC_CONTENT_TYPE
    assert classified.extension == ""


def test_longest_extension_wins():
    assert classify("x.fb2.zip", _ZIP).content_type == "application/x-zip-compressed-fb2"
    assert classify("x.tar.zip", _ZIP).content_type == "application/zip"
    assert classify("my.report.v2.pdf", b"%PDF-1.7").content_type == "application/pdf"


@pytest.mark.parametrize(
    ("content_type", "inline"),
    [
        ("application/pdf", True),
        ("text/plain; charset=utf-8", True),
        ("application/json; charset=utf-8", True),
        # Could run scripts if rendered from the storage origin.
        ("text/html; charset=utf-8", False),
        ("application/xml; charset=utf-8", False),
        ("application/x-fictionbook+xml", False),
        (GENERIC_CONTENT_TYPE, False),
        ("application/epub+zip", False),
    ],
)
def test_only_safe_formats_display_inline(content_type, inline):
    assert is_inline(content_type) is inline


def test_every_inline_type_is_a_recognized_format():
    recognized = {f.content_type for f in FORMATS.values()}
    for content_type in ("application/pdf", "text/plain", "application/json"):
        assert content_type in recognized


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        (r"C:\Users\me\Report Q3.PDF", "Report Q3.PDF"),
        ("/home/me/notes.txt", "notes.txt"),
        ("  spaced.pdf  ", "spaced.pdf"),
        ("", "file"),
        (None, "file"),
    ],
)
def test_clean_filename(raw, cleaned):
    assert clean_filename(raw) == cleaned


def test_clean_filename_caps_length_keeping_extension():
    cleaned = clean_filename("x" * 300 + ".pdf")

    assert len(cleaned) == 255
    assert cleaned.endswith(".pdf")


def test_utf8_is_judged_from_the_sniffed_sample_only():
    """Only the first SNIFF_BYTES are read back from storage; a multi-byte
    character cut off at the end of that sample doesn't make it non-UTF-8."""
    sample = ("я" * SNIFF_BYTES).encode()[: SNIFF_BYTES - 1]

    assert classify("long.txt", sample).content_type == "text/plain; charset=utf-8"


@pytest.mark.parametrize(
    ("filename", "extension", "content_type"),
    [
        ("Report.PDF", ".pdf", "application/pdf"),
        ("book.fb2.zip", ".fb2.zip", "application/x-zip-compressed-fb2"),
        ("notes.txt", ".txt", "text/plain"),
        ("setup.exe", "", GENERIC_CONTENT_TYPE),
        ("no-extension", "", GENERIC_CONTENT_TYPE),
    ],
)
def test_expected_format_comes_from_the_extension_alone(filename, extension, content_type):
    expected = expected_format(filename)

    assert (expected.extension, expected.content_type) == (extension, content_type)


@pytest.mark.parametrize(
    ("filename", "data", "content_type"),
    [
        ("clip.mp4", _MP4, "video/mp4"),
        ("clip.MOV", b"\x00\x00\x00\x08wide" + b"\x00" * 16, "video/quicktime"),
        ("clip.webm", _EBML, "video/webm"),
        ("film.mkv", _EBML, "video/x-matroska"),
        ("old.avi", b"RIFF\x00\x00\x00\x00AVI LIST", "video/x-msvideo"),
        ("tape.mpg", b"\x00\x00\x01\xba" + b"\x00" * 8, "video/mpeg"),
        ("song.mp3", b"ID3\x04\x00" + b"\x00" * 16, "audio/mpeg"),
        ("untagged.mp3", b"\xff\xfb\x90\x64" + b"\x00" * 16, "audio/mpeg"),
        ("voice.m4a", _MP4, "audio/mp4"),
        ("take.wav", b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
        ("album.flac", b"fLaC\x00\x00\x00\x22", "audio/flac"),
        ("memo.ogg", _OGG, "audio/ogg"),
        ("memo.opus", _OGG, "audio/ogg"),
    ],
)
def test_recognized_media_formats(filename, data, content_type):
    classified = classify(filename, data)

    assert classified.recognized
    assert classified.content_type == content_type
    # Nothing to extract text from.
    assert not classified.analyzable


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("renamed.mp4", b"MZ\x90\x00" + b"\x00" * 16),
        ("renamed.mp3", b"%PDF-1.7"),
        ("renamed.wav", b"RIFF\x00\x00\x00\x00AVI LIST"),  # an AVI, not a WAV
        ("renamed.webm", _OGG),
    ],
)
def test_media_that_doesnt_match_its_extension_is_generic(filename, data):
    assert classify(filename, data).content_type == GENERIC_CONTENT_TYPE


@pytest.mark.parametrize(
    ("filename", "data", "kind"),
    [
        ("Report.pdf", b"%PDF-1.7", ContentKind.document),
        ("notes.docx", _ZIP, ContentKind.document),
        ("todo.txt", b"buy milk", ContentKind.document),
        ("old.txt", "привет".encode("cp1251"), ContentKind.document),
        ("data.json", b'{"a": 1}', ContentKind.document),
        ("book.epub", _real_epub(), ContentKind.book),
        ("Война и мир.fb2", _FB2, ContentKind.book),
        ("book.fb2.zip", _ZIP, ContentKind.book),
        ("book.mobi", _MOBI, ContentKind.book),
        ("scan.djvu", b"AT&TFORM\x00\x00", ContentKind.book),
        ("clip.mp4", _MP4, ContentKind.video),
        ("voice.m4a", _MP4, ContentKind.audio),
        ("memo.ogg", _OGG, ContentKind.audio),
        ("stuff.zip", _ZIP, ContentKind.other),
        ("program.exe", b"MZ\x90\x00", ContentKind.other),
        ("fake.pdf", b"<html>not a pdf</html>", ContentKind.other),
    ],
)
def test_a_files_kind_follows_from_its_stored_content_type(filename, data, kind):
    assert kind_of(classify(filename, data).content_type) == kind


def test_every_format_has_exactly_one_kind():
    """Filtering and counting go by content type alone, so formats that
    share a type must share a kind too."""
    seen: dict[str, ContentKind] = {}
    for file_format in FORMATS.values():
        assert seen.setdefault(file_format.content_type, file_format.kind) == file_format.kind, file_format
    kinds = list(FILE_KIND_CONTENT_TYPES.values())
    for index, content_types in enumerate(kinds):
        for other in kinds[index + 1 :]:
            assert not content_types & other


def test_files_are_never_images():
    """Images are their own item type; an image format uploaded as a file
    is generic, and DjVu (`image/vnd.djvu`) is a book."""
    assert ContentKind.image not in FILE_KIND_CONTENT_TYPES
    assert kind_of("image/vnd.djvu") == ContentKind.book
    assert kind_of(classify("photo.heic", b"\x00\x00\x00\x18ftypheic").content_type) == ContentKind.other
