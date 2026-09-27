"""Processing limits: however large or hostile an upload, text extraction
reads, inflates, parses and keeps only a bounded amount of it. A limit
reached after some text keeps that text (partial); before any, it fails."""

import io
import random
import zipfile
import zlib

import pytest
from stash_worker_core.errors import ProcessingLimitExceeded

from document_analyzer.excerpt import SEPARATOR
from document_analyzer.parsers import DocumentSource, ExtractedText, ParserLimits, extract_text, parser_for

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


def _pdf(page_contents: list[bytes], *, extra_objects: list[bytes] = ()) -> bytes:
    """A PDF with one page per content stream (Helvetica as F1), plus any
    `extra_objects` no page refers to."""
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
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
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
