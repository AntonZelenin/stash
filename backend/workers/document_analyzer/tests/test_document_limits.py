"""Processing limits: however large or hostile an upload, text extraction
reads, inflates, parses and keeps only a bounded amount of it. A limit
reached after some text keeps that text (partial); before any, it fails."""

import io
import random
import struct
import tempfile
import zipfile
import zlib

import pytest
from stash_worker_core.errors import MalformedInputError, ProcessingLimitExceeded

from document_analyzer.excerpt import SEPARATOR
from document_analyzer.parsers import (
    DocumentSource,
    ExtractedText,
    ParserLimits,
    TextCollector,
    extract_text,
    parser_for,
)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MB = 1024 * 1024


def _run(content_type: str, data: bytes, **limits) -> tuple[ExtractedText, DocumentSource]:
    source = DocumentSource.from_bytes(data)
    return extract_text(parser_for(content_type), source, ParserLimits(**limits)), source


def _zip(members: dict[str, bytes | str], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _docx(paragraphs: int, *, extra: dict[str, bytes] | None = None) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>Paragraph {i}</w:t></w:r></w:p>" for i in range(paragraphs))
    return _zip({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>', **(extra or {})})


def _pdf(page_contents: list[bytes], *, extra_objects: list[bytes] = (), xobjects: str = "") -> bytes:
    """A PDF with one page per content stream (Helvetica as F1), plus any
    `extra_objects`, numbered from `_first_extra(len(page_contents))`, which
    pages refer to only through `xobjects` (e.g. "/Fm1 9 0 R"), their
    /XObject resources."""
    count = len(page_contents)
    font = 3 + 2 * count
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + b" ".join(f"{3 + 2 * i} 0 R".encode() for i in range(count))
        + f"] /Count {count} >>".encode(),
    ]
    for index, content in enumerate(page_contents):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * index} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> /XObject << {xobjects} >> >> >>".encode()
        )
        objects.append(content)
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.extend(extra_objects)

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


def _first_extra(pages: int) -> int:
    """The object number of `_pdf`'s first extra object."""
    return 4 + 2 * pages


def _form(content: bytes, *, pages: int, flate: bool = False) -> bytes:
    """A form XObject drawing `content`, for the `extra_objects` of a `_pdf`
    of `pages` pages, whose font it uses (pypdf skips forms without
    resources)."""
    header = f"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /Font << /F1 {3 + 2 * pages} 0 R >> >> "
    return _stream(content, flate=flate).replace(b"<< ", header.encode(), 1)


def _stream(data: bytes, *, flate: bool = False) -> bytes:
    if flate:
        data = zlib.compress(data)
        return f"<< /Length {len(data)} /Filter /FlateDecode >>\nstream\n".encode() + data + b"\nendstream"
    return f"<< /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"


def _page_text(text: str, *, padding: int = 0) -> bytes:
    """A content stream showing `text`, padded with a PDF comment."""
    return _stream(f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET\n".encode() + b"%" + b"x" * padding + b"\n")


# ---- ZIP-based formats ----


def test_zip_bomb_stops_at_the_decompression_limit_keeping_text_so_far():
    # ~50 MB of paragraphs inflates from well under 1 MB.
    body = "<w:p><w:r><w:t>Paragraph 0</w:t></w:r></w:p><w:p><w:r><w:t>Paragraph 1</w:t></w:r></w:p>"
    body += "<w:p><w:r><w:t>Again</w:t></w:r></w:p>" * 1_300_000
    data = _zip({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'})
    assert len(data) < 1 * MB

    extracted, _ = _run(DOCX, data, max_uncompressed_bytes=1 * MB)

    assert extracted.is_partial
    assert "Decompressed" in extracted.stopped_by
    assert extracted.text.startswith("Paragraph 0\nParagraph 1\n")
    assert len(extracted.text) < 1 * MB


def test_zip_bomb_with_no_text_before_the_limit_fails():
    data = _zip({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body>' + " " * (50 * MB)})

    with pytest.raises(ProcessingLimitExceeded):
        _run(DOCX, data, max_uncompressed_bytes=1 * MB)


def test_archive_with_too_many_entries_is_refused_before_its_directory_is_read():
    data = _docx(1, extra={f"junk/{i}.xml": b"" for i in range(2_000)})

    with pytest.raises(ProcessingLimitExceeded, match="entries"):
        _run(DOCX, data, max_archive_entries=100)


def test_only_the_needed_parts_of_a_large_archive_are_read():
    # 30 MB of (incompressible) media, and the text.
    media = {f"word/media/image{i}.png": random.Random(i).randbytes(10 * MB) for i in range(3)}
    data = _docx(10, extra=media)
    assert len(data) > 20 * MB

    extracted, source = _run(DOCX, data)

    assert "Paragraph 9" in extracted.text
    assert not extracted.is_partial
    assert source.bytes_read < 1 * MB


def test_archive_reads_stop_at_the_read_limit():
    data = _zip({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body><w:p>' + "x" * (8 * MB)},
                compression=zipfile.ZIP_STORED)

    with pytest.raises(ProcessingLimitExceeded):
        _run(DOCX, data, max_bytes_read=1 * MB)


# ---- PDF ----


def test_pdf_reads_at_most_the_page_limit_spread_over_the_document():
    data = _pdf([_page_text(f"Page {n}") for n in range(40)])

    extracted, _ = _run("application/pdf", data, max_pdf_pages=6)

    pages = [int(word) for word in extracted.text.split() if word.isdigit()]
    assert pages[:3] == [0, 1, 2]  # the beginning
    assert len(pages) == 6 and pages == sorted(pages) and pages[-1] >= 30  # then spread over the rest
    assert extracted.is_partial


def test_pdf_is_downloaded_in_chunks_never_whole_into_memory():
    data = _pdf([_page_text("Invoice")], extra_objects=[_stream(b"\x00" * (20 * MB))])
    fetches = []
    source = DocumentSource(lambda start, end: fetches.append(end - start) or data[start:end], len(data))

    extracted = extract_text(parser_for("application/pdf"), source, ParserLimits())

    assert "Invoice" in extracted.text
    assert sum(fetches) == len(data)
    assert max(fetches) <= 8 * MB


def test_pdf_over_the_download_limit_is_refused_without_downloading_it():
    data = _pdf([_page_text("Invoice", padding=2 * MB)])
    fetches = []
    source = DocumentSource(lambda start, end: fetches.append(end) or data[start:end], len(data))
    with pytest.raises(ProcessingLimitExceeded, match="download"):
        extract_text(parser_for("application/pdf"), source, ParserLimits(max_download_bytes=1 * MB))
    assert fetches == []


def test_pdf_decompression_bomb_is_stopped():
    # 200 MB of spaces, compressed to a fraction of a megabyte.
    bomb = _stream(b"BT /F1 24 Tf 72 720 Td (Boom) Tj ET\n" + b" " * (200 * MB), flate=True)

    with pytest.raises(ProcessingLimitExceeded):
        _run("application/pdf", _pdf([bomb]), max_uncompressed_bytes=1 * MB)


def test_pdf_decompression_bomb_after_text_keeps_that_text():
    bomb = _stream(b" " * (200 * MB), flate=True)

    extracted, _ = _run("application/pdf", _pdf([_page_text("Contract"), bomb]), max_uncompressed_bytes=1 * MB)

    assert "Contract" in extracted.text and extracted.is_partial


# ---- formats read from the start ----


def test_large_plain_text_is_read_only_where_excerpts_come_from():
    data = "\n".join(f"line {i} of a very long log file" for i in range(1_500_000)).encode()
    assert len(data) > 40 * MB

    extracted, source = _run("text/plain", data, excerpt_chars=24_000)

    assert extracted.text.startswith("line 0 of")
    assert extracted.text.count(SEPARATOR) == 4
    assert len(extracted.text) <= 24_000
    assert extracted.is_partial
    assert source.bytes_read < 1 * MB


def test_large_legacy_encoded_text_samples_decode_in_its_encoding():
    data = ("Список покупок на неделю. " * 1_000_000).encode("cp1251")

    extracted, _ = _run("text/plain", data, max_prefix_bytes=1 * MB, excerpt_chars=4_000)

    assert extracted.text.count("Список") > 20
    assert "�" not in extracted.text


def test_html_beyond_the_prefix_limit_is_not_read():
    data = b"<html><body>" + b"<p>Release notes</p>" * 200_000 + b"</body></html>"

    extracted, source = _run("text/html", data, max_prefix_bytes=1 * MB)

    assert "Release notes" in extracted.text
    assert extracted.is_partial
    assert source.bytes_read == 1 * MB


def test_xml_cut_at_the_prefix_limit_keeps_the_text_before_the_cut():
    data = b"<log>" + b"<entry>disk full</entry>" * 200_000 + b"</log>"

    extracted, source = _run("application/xml", data, max_prefix_bytes=1 * MB)

    assert extracted.text.startswith("disk full\ndisk full\n")
    assert extracted.is_partial
    assert source.bytes_read == 1 * MB


def test_malformed_xml_within_the_prefix_is_still_an_error():
    with pytest.raises(Exception):
        _run("application/xml", b"<a><b></a>")


def test_fb2_book_beyond_the_prefix_limit():
    body = "".join(f"<p>Sentence {i}.</p>" for i in range(200_000))
    data = f"<FictionBook><body><section>{body}</section></body></FictionBook>".encode()

    extracted, _ = _run("application/x-fictionbook+xml", data, max_prefix_bytes=256 * 1024)

    assert extracted.text.startswith("Sentence 0.\nSentence 1.\n")
    assert extracted.is_partial


def test_rtf_beyond_the_prefix_limit():
    data = b"{\\rtf1\\ansi " + b"Minutes of the meeting. " * 200_000 + b"}"

    extracted, source = _run("application/rtf", data, max_prefix_bytes=256 * 1024)

    assert "Minutes of the meeting." in extracted.text
    assert extracted.is_partial and source.bytes_read == 256 * 1024


# ---- text and time ----


def test_extracted_text_is_capped():
    extracted, _ = _run(DOCX, _docx(10_000), max_text_chars=1_000)

    assert len(extracted.text) == 1_000
    assert extracted.is_partial and "characters" in extracted.stopped_by


def test_one_enormous_paragraph_is_capped_too():
    data = _zip({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body><w:p>'
                 + "<w:r><w:t>word </w:t></w:r>" * 200_000 + "</w:p></w:body></w:document>"})

    extracted, _ = _run(DOCX, data, max_text_chars=10_000)

    assert len(extracted.text) == 10_000 and extracted.is_partial


def test_running_out_of_time_keeps_the_text_so_far():
    extracted, _ = _run("text/plain", b"a short note", timeout_seconds=-1)

    assert extracted.text == "a short note"
    assert extracted.is_partial and "time" in extracted.stopped_by


def test_running_out_of_time_before_any_text_fails():
    with pytest.raises(ProcessingLimitExceeded, match="time"):
        _run("application/pdf", _pdf([_page_text("Late")]), timeout_seconds=-1)


# ---- ZIP entries: checked from the directory, before inflating ----


def _paragraphs_xml(*texts: str, padding: str = "") -> str:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in texts)
    return f'<w:document xmlns:w="{_W}"><w:body>{body}{padding}</w:body></w:document>'


def test_member_compressing_past_the_ratio_limit_stops_there_keeping_text_so_far():
    # ~1000:1, like a zip bomb: 20 MB of spaces, well within the archive's
    # decompression budget, inflates no further than 200x its size.
    data = _zip({"word/document.xml": _paragraphs_xml("Title", "Intro", padding=" " * (20 * MB))})
    compressed = zipfile.ZipFile(io.BytesIO(data)).getinfo("word/document.xml").compress_size
    assert 20 * MB / compressed > 500

    extracted, _ = _run(DOCX, data)

    assert extracted.text.startswith("Title\nIntro\n")
    assert extracted.is_partial and "200:1" in extracted.stopped_by


def test_zip_bomb_member_with_no_text_before_the_ratio_limit_fails():
    data = _zip({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body>' + " " * (20 * MB)})

    with pytest.raises(ProcessingLimitExceeded, match="200:1"):
        _run(DOCX, data)


def test_small_members_may_compress_however_well():
    # Highly repetitive, but under the 1 MB grace: read whole.
    data = _zip({"word/document.xml": _paragraphs_xml(*["Same line"] * 20_000)})

    extracted, _ = _run(DOCX, data, max_compression_ratio=2)

    assert extracted.text.count("Same line") == 20_000 and not extracted.is_partial


def test_oversized_uncompressed_member_stops_at_the_member_limit():
    # Stored, so no ratio: ~3 MB of paragraphs, over a 1 MB member limit.
    data = _zip({"word/document.xml": _paragraphs_xml(*(f"Paragraph {i}" for i in range(80_000)))},
                compression=zipfile.ZIP_STORED)

    extracted, source = _run(DOCX, data, max_member_bytes=1 * MB, max_text_chars=10 * MB)

    assert extracted.text.startswith("Paragraph 0\n")
    assert extracted.is_partial and f"past {1 * MB} bytes" in extracted.stopped_by
    assert source.bytes_read < 2 * MB


def _declaring_entries(data: bytes, entries: int) -> bytes:
    """`data`, a ZIP, with its end-of-directory record claiming `entries`."""
    at = data.rfind(b"PK\x05\x06")
    record = bytearray(data[at:])
    struct.pack_into("<2H", record, 8, entries, entries)
    return data[:at] + bytes(record)


def test_directory_listing_more_entries_than_declared_is_refused():
    data = _declaring_entries(_docx(1, extra={f"junk/{i}.xml": b"" for i in range(500)}), 1)

    with pytest.raises(ProcessingLimitExceeded, match="directory"):
        _run(DOCX, data, max_archive_entries=100)


@pytest.mark.parametrize("name", ["../evil.xml", "word/../../evil.xml", "/etc/evil.xml", "C:/evil.xml", "..\\evil.xml"])
def test_archive_with_a_member_outside_itself_is_malformed(name):
    data = _docx(3, extra={name: b"<x/>"})

    with pytest.raises(MalformedInputError, match="outside the archive"):
        _run(DOCX, data)


def test_encrypted_member_is_malformed_and_never_inflated():
    data = bytearray(_zip({"word/document.xml": _paragraphs_xml("Secret")}))
    # Flagged encrypted, in its local header and directory entry (its bytes
    # aren't, but they're never looked at).
    for signature, flags_at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        data[data.index(signature) + flags_at] |= 0x1

    with pytest.raises(MalformedInputError, match="encrypted"):
        _run(DOCX, bytes(data))


def test_xml_element_limit_stops_parsing_keeping_text_so_far():
    # Text first, then a flood of empty paragraphs: no text, all work.
    data = _zip({"word/document.xml": _paragraphs_xml("Heading", padding="<w:p/>" * 50_000)})

    extracted, _ = _run(DOCX, data, max_xml_elements=10_000)

    assert extracted.text.strip() == "Heading"
    assert extracted.is_partial and "XML elements" in extracted.stopped_by


def test_parsing_stops_once_enough_text_is_extracted():
    # 8 MB stored: only its beginning is read, the text limit reached.
    data = _zip({"word/document.xml": _paragraphs_xml(*(f"Paragraph {i}" for i in range(200_000)))},
                compression=zipfile.ZIP_STORED)
    assert len(data) > 8 * MB

    extracted, source = _run(DOCX, data, max_text_chars=10_000)

    assert len(extracted.text) == 10_000 and "characters" in extracted.stopped_by
    assert source.bytes_read < 1 * MB


# ---- PDF: work per page, pages in all, opening ----


def _operators(count: int) -> bytes:
    return b"1 0 0 1 0 0 cm\n" * count


def test_pdf_page_with_too_much_content_is_skipped_keeping_the_others():
    # A few KB of PDF whose second page parses 1 MB of operators.
    heavy = _stream(b"BT /F1 24 Tf 72 720 Td (Heavy) Tj ET\n" + _operators(70_000), flate=True)
    data = _pdf([_page_text("First"), heavy, _page_text("Third")])
    assert len(data) < 20_000

    extracted, _ = _run("application/pdf", data, max_pdf_page_content_bytes=64 * 1024)

    assert "First" in extracted.text and "Third" in extracted.text and "Heavy" not in extracted.text
    assert extracted.is_partial


def test_pdf_whose_every_page_has_too_much_content_fails():
    heavy = _stream(b"BT /F1 24 Tf 72 720 Td (Heavy) Tj ET\n" + _operators(70_000), flate=True)

    with pytest.raises(ProcessingLimitExceeded, match="content"):
        _run("application/pdf", _pdf([heavy]), max_pdf_page_content_bytes=64 * 1024)


def test_form_xobject_content_counts_toward_its_page():
    # The page itself is tiny; the form it draws is not.
    form = _first_extra(1)
    page = _stream(b"BT /F1 24 Tf 72 720 Td (Cover) Tj ET /Fm1 Do")
    data = _pdf([page], extra_objects=[_form(_operators(70_000), pages=1, flate=True)], xobjects=f"/Fm1 {form} 0 R")

    with pytest.raises(ProcessingLimitExceeded, match="content"):
        _run("application/pdf", data, max_pdf_page_content_bytes=64 * 1024)


def test_form_xobject_drawn_again_and_again_counts_every_time():
    # 1 KB of form content, drawn 200 times: 200 KB parsed for one page.
    form = _first_extra(1)
    page = _stream(b"BT /F1 24 Tf 72 720 Td (Pattern) Tj ET\n" + b"/Fm1 Do\n" * 200)
    data = _pdf([page], extra_objects=[_form(_operators(70), pages=1)], xobjects=f"/Fm1 {form} 0 R")

    assert "Pattern" in _run("application/pdf", data)[0].text
    with pytest.raises(ProcessingLimitExceeded, match="content"):
        _run("application/pdf", data, max_pdf_page_content_bytes=64 * 1024)


def test_pdf_with_too_many_pages_in_all_is_refused():
    with pytest.raises(ProcessingLimitExceeded, match="page tree"):
        _run("application/pdf", _pdf([_page_text(f"Page {n}") for n in range(300)]), max_pdf_page_count=100)


def _with_cross_references(data: bytes, entries: int) -> bytes:
    """`data`, a PDF from `_pdf`, with its cross-reference table padded to
    `entries` entries (all pointing at its catalog)."""
    head, _, _ = data.rpartition(b"xref\n")
    catalog = head.index(b"1 0 obj")
    table = f"xref\n0 {entries + 1}\n0000000000 65535 f \n".encode() + f"{catalog:010d} 00000 n \n".encode() * entries
    trailer = f"trailer << /Size {entries + 1} /Root 1 0 R >>\nstartxref\n{len(head)}\n%%EOF\n".encode()
    return head + table + trailer


def test_oversized_cross_reference_data_is_refused_while_opening():
    # 200,000 entries: 4 MB of file, and ten times that as Python objects.
    data = _with_cross_references(_pdf([_page_text("Hello")]), 200_000)

    with pytest.raises(ProcessingLimitExceeded, match="opening the PDF"):
        _run("application/pdf", data, max_pdf_open_bytes=1 * MB)


def test_pdf_reads_stop_at_the_read_limit():
    # Each page draws its own 1 MB image, which pypdf reads (never decodes)
    # to see what it is.
    pages = 8
    first = _first_extra(pages)
    image = b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 "
    images = [image + f"/Length {MB} >>\nstream\n".encode() + bytes(MB) + b"\nendstream" for _ in range(pages)]
    contents = [_stream(f"BT /F1 24 Tf 72 720 Td (Scan {n}) Tj ET /Im{n} Do".encode()) for n in range(pages)]
    data = _pdf(contents, extra_objects=images, xobjects=" ".join(f"/Im{n} {first + n} 0 R" for n in range(pages)))

    extracted, _ = _run("application/pdf", data, max_bytes_read=3 * MB)

    assert "Scan 0" in extracted.text and "Scan 7" not in extracted.text
    assert extracted.is_partial and "Read over" in extracted.stopped_by


def test_pdf_font_files_count_toward_the_decompression_limit():
    # A Type1 font without /ToUnicode: pypdf decodes its embedded font file.
    first = _first_extra(1)
    font = (f"<< /Type /Font /Subtype /Type1 /BaseFont /Bomb /FontDescriptor {first + 1} 0 R >>").encode()
    descriptor = (f"<< /Type /FontDescriptor /FontName /Bomb /Flags 32 /FontFile {first + 2} 0 R >>").encode()
    page = _stream(b"BT /F2 24 Tf 72 720 Td (Styled) Tj ET")
    data = _pdf([page], extra_objects=[font, descriptor, _stream(bytes(8 * MB), flate=True)])
    data = data.replace(b"/Font << /F1", f"/Font << /F2 {first} 0 R /F1".encode())

    with pytest.raises(ProcessingLimitExceeded, match="Decompressed"):
        _run("application/pdf", data, max_uncompressed_bytes=1 * MB)


def test_time_limit_is_checked_within_a_page():
    """A clock that advances a second per look: time runs out while the
    only page is being extracted, which only checks within it can see
    (between pages, it would be done with a second to spare)."""
    ticks = iter(range(1_000_000))
    limits = ParserLimits(timeout_seconds=5)
    text = TextCollector(limits, clock=lambda: next(ticks))
    page = _stream(b"BT /F1 24 Tf 72 720 Td (Slow) Tj ET\n" + _operators(20_000))

    with pytest.raises(ProcessingLimitExceeded, match="time"):
        parser_for("application/pdf").extract(DocumentSource.from_bytes(_pdf([page])), limits, text)


def test_pypdf_hooks_are_installed():
    """If a pypdf upgrade moves what these wrap, the budget tests above
    fail too; this says why."""
    import pypdf.filters
    from pypdf.generic import ContentStream

    from document_analyzer import parsers

    assert pypdf.filters.decode_stream_data is parsers._counted_decode_stream_data
    assert ContentStream._parse_content_stream is parsers._counted_parse_content_stream


def test_pdf_temporary_file_is_removed_on_success_and_on_failure(monkeypatch):
    opened = []
    temporary_file = tempfile.TemporaryFile

    def tracked(*args, **kwargs):
        opened.append(temporary_file(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(tempfile, "TemporaryFile", tracked)
    heavy = _stream(_operators(70_000), flate=True)

    _run("application/pdf", _pdf([_page_text("Fine")]))
    with pytest.raises(ProcessingLimitExceeded):
        _run("application/pdf", _pdf([heavy]), max_pdf_page_content_bytes=64 * 1024)

    assert len(opened) == 2 and all(file.closed for file in opened)
