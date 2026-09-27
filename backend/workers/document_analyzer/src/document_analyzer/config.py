from functools import lru_cache

from stash_worker_core.config import WorkerSettings


class Settings(WorkerSettings):
    openai_api_key: str = ""
    # AWS: the Secrets Manager secret holding the key; replaces
    # `openai_api_key` when set.
    openai_api_key_secret_arn: str = ""
    openai_model: str = "gpt-6-luna"
    openai_timeout_seconds: float = 90.0
    # Most extracted document text sent to OpenAI per document, in
    # characters (~4 per token). Longer documents are represented by
    # excerpts (see `document_analyzer.excerpt`). Independent of the upload
    # size limit.
    document_analysis_max_chars: int = 24_000

    # Processing limits: how much work extracting one document's text may
    # take, however large the upload (files may be up to 500 MB). See
    # `document_analyzer.parsers` for how each applies. Past one, the text
    # extracted so far is described (as partial), or, with none yet, the
    # document fails (PROCESSING_LIMIT_EXCEEDED, never retried). Sized for
    # the function's 512 MB of memory: a PDF, the worst case, holds at most
    # ~80 MB opening it, 56 MB more read, 64 MB decompressed and ~80 MB
    # parsing one page, plus one stream being decompressed (16 MB).
    # Formats read from the start (HTML, XML, FB2, RTF): most bytes read.
    # Plain text up to this size is read whole; larger, only where its
    # excerpts come from.
    document_max_prefix_bytes: int = 8 * 1024 * 1024
    # ZIP-based formats (read at random): most bytes fetched. PDF: most
    # bytes pypdf reads of the downloaded file (it keeps what it read).
    document_max_bytes_read: int = 64 * 1024 * 1024
    # PDFs need random access to the whole file: the largest downloaded,
    # to local disk (the function's ephemeral storage, 1 GB), never into
    # memory. Above the upload limit, so no PDF is refused for its size.
    document_max_download_bytes: int = 512 * 1024 * 1024
    # ZIP-based formats and PDF: most bytes decompressed, all members or
    # streams together.
    document_max_uncompressed_bytes: int = 64 * 1024 * 1024
    # ZIP-based formats: most entries, as declared and as listed.
    document_max_archive_entries: int = 10_000
    # ZIP-based formats: most bytes any one member inflates to, and how
    # much better than this it may compress (members over 1 MB). Office
    # XML compresses 10-50:1; a zip bomb ~1000:1.
    document_max_member_bytes: int = 64 * 1024 * 1024
    document_max_compression_ratio: int = 200
    # XML in any format: most elements parsed, all parts together.
    document_max_xml_elements: int = 2_000_000
    # PDF: pages whose text is read (spread over the document).
    document_max_pdf_pages: int = 50
    # PDF: most pages in all. pypdf lists every page as it opens a
    # document (~0.3 ms each, uninterruptible), so larger ones fail.
    document_max_pdf_page_count: int = 10_000
    # PDF: most bytes read and decompressed to open it (cross-references,
    # page tree), which pypdf holds at ~10x their size.
    document_max_pdf_open_bytes: int = 8 * 1024 * 1024
    # PDF: most bytes of content a page's text is extracted from (its own
    # and its form XObjects'). pypdf parses content in one uninterruptible
    # call, at ~5 s and ~40 MB per MB; larger pages are skipped. Text pages
    # are 10-100 KB.
    document_max_pdf_page_content_bytes: int = 2 * 1024 * 1024
    # PDF: most bytes any one stream decompresses to. Extracting text
    # decompresses only content (skipped past the limit above anyway),
    # fonts and cross-reference data.
    document_max_pdf_stream_bytes: int = 16 * 1024 * 1024
    # Most characters of text extracted before parsing stops: 10x what's
    # sent to OpenAI (document_analysis_max_chars), so the excerpts still
    # come from across a long document, without extracting all of it.
    document_max_text_chars: int = 240_000
    # Wall-clock time for extraction, well within the function's timeout
    # (worker_timeout_seconds, 180 s) with the OpenAI call (90 s) after it,
    # leaving room for one uninterruptible parser call to overrun it.
    document_parse_timeout_seconds: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
