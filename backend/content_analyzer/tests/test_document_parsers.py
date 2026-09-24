import io
import zipfile

import pytest
from pypdf import PdfWriter

from content_analyzer.documents import parsers
from content_analyzer.documents.parsers import normalize_text, parser_for, supported_content_types

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ODT = "application/vnd.oasis.opendocument.text"


def _zip(members: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _extract(content_type: str, data: bytes) -> str:
    parser = parser_for(content_type)
    assert parser is not None, content_type
    return normalize_text(parser.extract_text(data))


def _pdf_with_text(text: str) -> bytes:
    """A minimal one-page PDF showing `text` in Helvetica."""
    content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode())
    return out.getvalue()


# ---- formats ----


def test_pdf():
    assert "Quarterly budget review" in _extract("application/pdf", _pdf_with_text("Quarterly budget review"))


def test_password_protected_pdf_is_unparsable():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(user_password="secret")
    buffer = io.BytesIO()
    writer.write(buffer)

    with pytest.raises(parsers.UnparsableDocumentError):
        parser_for("application/pdf").extract_text(buffer.getvalue())


def test_docx_paragraphs_in_order():
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = (
        f'<w:document xmlns:w="{w}"><w:body>'
        "<w:p><w:r><w:t>Employment </w:t></w:r><w:r><w:t>contract</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Salary table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "<w:p><w:r><w:t>Signed in Lisbon</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    text = _extract(DOCX, _zip({"word/document.xml": document}))

    assert text == "Employment contract\nSalary table cell\nSigned in Lisbon"


def test_pptx_slides_in_numeric_order():
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"

    def slide(text):
        return f'<p:sld xmlns:p="p" xmlns:a="{a}"><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>'

    data = _zip({"ppt/slides/slide10.xml": slide("Ten"), "ppt/slides/slide2.xml": slide("Two"),
                 "ppt/slides/slide1.xml": slide("One")})

    assert _extract(PPTX, data) == "One\nTwo\nTen"


def test_xlsx_shared_strings():
    strings = '<sst xmlns="s"><si><t>Region</t></si><si><r><t>North </t></r><r><t>America</t></r></si></sst>'

    assert _extract(XLSX, _zip({"xl/sharedStrings.xml": strings})) == "Region\nNorth America"


def test_odt_headings_and_paragraphs():
    content = (
        '<office:document-content xmlns:office="o" xmlns:text="t"><office:body><office:text>'
        "<text:h>Meeting notes</text:h><text:p>Agenda: <text:span>hiring</text:span></text:p>"
        "</office:text></office:body></office:document-content>"
    )

    assert _extract(ODT, _zip({"content.xml": content})) == "Meeting notes\nAgenda: hiring"


def test_epub_follows_spine_not_file_names():
    container = (
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    opf = (
        '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
        '<item id="a" href="text/b_first.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="b" href="text/a_second.xhtml" media-type="application/xhtml+xml"/>'
        '</manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>'
    )
    data = _zip({
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": container,
        "OEBPS/content.opf": opf,
        "OEBPS/text/b_first.xhtml": "<html><body><h1>Chapter One</h1><p>It begins.</p></body></html>",
        "OEBPS/text/a_second.xhtml": "<html><body><h1>Chapter Two</h1><script>x()</script></body></html>",
    })

    assert _extract("application/epub+zip", data) == "Chapter One\n\nIt begins.\n\nChapter Two"


def test_fb2_metadata_then_body_in_declared_encoding():
    fb2 = (
        '<?xml version="1.0" encoding="windows-1251"?>'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description><title-info>'
        "<genre>prose_classic</genre><author><first-name>Лев</first-name><last-name>Толстой</last-name></author>"
        "<book-title>Война и мир</book-title></title-info></description>"
        "<body><section><title><p>Часть первая</p></title><p>Ну что, князь.</p></section></body>"
        "<binary id='cover.jpg'>AAAA</binary></FictionBook>"
    ).encode("cp1251")

    text = _extract("application/x-fictionbook+xml", fb2)

    assert text == "prose_classic\nЛев Толстой\nВойна и мир\nЧасть первая\nНу что, князь."


def test_fb2_zip():
    fb2 = '<FictionBook><body><p>Inside the archive</p></body></FictionBook>'

    assert _extract("application/x-zip-compressed-fb2", _zip({"book.fb2": fb2})) == "Inside the archive"


def test_rtf_uses_declared_code_page():
    # "мир" as Windows-1251 escapes.
    rtf = b"{\\rtf1\\ansi\\ansicpg1251 Hello \\'ec\\'e8\\'f0\\par}"

    assert _extract("application/rtf", rtf) == "Hello мир"


def test_html_skips_scripts_and_styles():
    html = b"<html><head><style>p{}</style><title>Release notes</title></head><body><script>evil()</script><p>v2.0</p></body></html>"

    assert _extract("text/html; charset=utf-8", html) == "Release notes\n\nv2.0"


def test_plain_text_in_legacy_encoding():
    data = "Список покупок: молоко, хлеб, яйца и сыр на неделю".encode("cp1251")

    assert _extract("text/plain", data) == "Список покупок: молоко, хлеб, яйца и сыр на неделю"


def test_xml_text_without_markup():
    assert _extract("application/xml", b"<config><name>stash</name><env>dev</env></config>") == "stash\ndev"


# ---- safety and failure modes ----


def test_xml_entity_expansion_is_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><x>&b;</x>'

    with pytest.raises(Exception):
        parser_for("application/xml").extract_text(bomb)


def test_oversized_zip_member_is_refused(monkeypatch):
    monkeypatch.setattr(parsers, "_MAX_ZIP_MEMBER_BYTES", 10)
    data = _zip({"word/document.xml": "<w:document xmlns:w='w'><w:p>" + "x" * 100 + "</w:p></w:document>"})

    with pytest.raises(parsers.UnparsableDocumentError):
        parser_for(DOCX).extract_text(data)


@pytest.mark.parametrize("content_type", [DOCX, "application/epub+zip", "application/pdf"])
def test_corrupt_input_raises(content_type):
    with pytest.raises(Exception):
        parser_for(content_type).extract_text(b"definitely not a real document")


def test_unsupported_types_have_no_parser():
    assert parser_for("application/octet-stream") is None
    assert parser_for("application/msword") is None


def test_every_analyzable_api_format_has_a_parser():
    """Must match the formats `app.items.files` on the API side marks
    `analyzable` (and therefore enqueues) — a missing parser would
    dead-letter every such upload."""
    analyzable = {
        "application/pdf", DOCX, XLSX, PPTX, ODT,
        "application/vnd.oasis.opendocument.spreadsheet", "application/vnd.oasis.opendocument.presentation",
        "application/rtf", "application/epub+zip", "application/x-fictionbook+xml",
        "application/x-zip-compressed-fb2", "text/plain", "text/markdown", "text/csv",
        "text/tab-separated-values", "application/json", "application/yaml", "application/xml", "text/html",
    }

    assert analyzable <= supported_content_types()


def test_normalize_text():
    assert normalize_text("  Title \r\n\r\n\r\n\tbody  text\x00  \n\n\n end ") == "Title\n\nbody text\n\nend"
