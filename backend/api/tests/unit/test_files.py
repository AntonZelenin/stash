import zipfile
from io import BytesIO

import pytest

from app.items.files import FORMATS, GENERIC_CONTENT_TYPE, classify, clean_filename, is_inline

_ZIP = b"PK\x03\x04" + b"\x00" * 64
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
_MOBI = b"\x00" * 60 + b"BOOKMOBI" + b"\x00" * 32
_FB2 = (
    '<?xml version="1.0" encoding="windows-1251"?>\n<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
    "<body><p>Глава первая</p></body></FictionBook>"
).encode("cp1251")


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
