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
    # document fails. Sized for the function's 512 MB of memory.
    # Formats read from the start (HTML, XML, FB2, RTF): most bytes read.
    # Plain text up to this size is read whole; larger, only where its
    # excerpts come from.
    document_max_prefix_bytes: int = 8 * 1024 * 1024
    # ZIP-based formats (read at random): most bytes fetched.
    document_max_bytes_read: int = 64 * 1024 * 1024
    # PDFs need random access to the whole file: the largest downloaded,
    # to local disk (the function's ephemeral storage, 1 GB), never into
    # memory. Above the upload limit, so no PDF is refused for its size.
    document_max_download_bytes: int = 512 * 1024 * 1024
    # ZIP-based formats: most bytes decompressed, all members together.
    # PDF: most bytes any one stream decompresses to.
    document_max_uncompressed_bytes: int = 64 * 1024 * 1024
    document_max_archive_entries: int = 10_000
    document_max_pdf_pages: int = 50
    # Most characters of text extracted before parsing stops.
    document_max_text_chars: int = 1_000_000
    # Wall-clock time for extraction, well within the function's timeout
    # (worker_timeout_seconds, 180 s) with the OpenAI call (90 s) after it.
    document_parse_timeout_seconds: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
