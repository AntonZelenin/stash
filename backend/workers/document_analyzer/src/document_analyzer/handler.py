import asyncio

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared import tracing
from stash_shared.log import get_logger
from stash_shared.outbox import OutboxPublisher
from stash_shared.queue.base import ProcessingJob
from stash_worker_core.completion import embedding_job_for, log_completion
from stash_worker_core.errors import PermanentProcessingError
from stash_worker_core.items import complete_item
from stash_worker_core.storage import ObjectStore

from document_analyzer.describer import DocumentDescriber
from document_analyzer.excerpt import select_excerpt
from document_analyzer.parsers import normalize_text, parser_for

logger = get_logger(__name__)
_tracer = trace.get_tracer(__name__)


class DocumentAnalysisHandler:
    """The document-analysis stage (`DOCUMENT_ANALYSIS_JOBS`): extracts the
    file's text, sends it (or representative excerpts of it, past
    `max_chars`) to the describer, and completes the item with the result as
    its description (after any caption, like images).

    Permanent, so dead-lettered without retries: an unsupported format, a
    file that can't be parsed (corrupt, encrypted...), or one with no
    extractable text (e.g. a scanned PDF — there's no OCR). Parsing is
    deterministic, so retrying those could only fail the same way.

    Safe to re-run: `complete_item` only writes if the item isn't finished
    yet, so a redelivered job costs at most a repeated OpenAI call.

    Its job ends with the description: completing the item also adds an
    `EMBEDDING_JOBS` job to the outbox, in the same transaction, which it
    then publishes via `outbox`; it never embeds itself.
    """

    def __init__(
        self,
        *,
        storage: ObjectStore,
        describer: DocumentDescriber,
        engine: AsyncEngine,
        max_chars: int,
        outbox: OutboxPublisher,
    ):
        self._storage = storage
        self._describer = describer
        self._engine = engine
        self._max_chars = max_chars
        self._outbox = outbox

    async def handle(self, job: ProcessingJob) -> None:
        if job.file is None:
            raise PermanentProcessingError("Document job has no file reference")
        parser = parser_for(job.file.content_type)
        if parser is None:
            raise PermanentProcessingError(f"No parser for {job.file.content_type!r}")

        data = await self._storage.download(job.file.storage_key)
        with _tracer.start_as_current_span("document.extract_text") as span:
            tracing.set_attributes(
                span, parser=type(parser).__name__, content_type=job.file.content_type, size_bytes=len(data)
            )
            try:
                raw_text = await asyncio.to_thread(parser.extract_text, data)
            except Exception as exc:
                raise PermanentProcessingError(f"Could not extract text: {exc!r}") from exc
            text = normalize_text(raw_text)
            span.set_attribute("text_chars", len(text))
        if not text:
            raise PermanentProcessingError("Document has no extractable text")

        excerpt = select_excerpt(text, self._max_chars)
        logger.info(
            "Document text extracted",
            storage_key=job.file.storage_key,
            content_type=job.file.content_type,
            parser=type(parser).__name__,
            size_bytes=len(data),
            text_chars=len(text),
            excerpt_chars=len(excerpt.text),
            is_partial=excerpt.is_partial,
        )
        description = await self._describer.describe(
            filename=job.file.filename, text=excerpt.text, is_partial=excerpt.is_partial
        )
        completed = await complete_item(
            self._engine, job.item_id, description=description, embedding_job=embedding_job_for(job)
        )
        log_completion(completed, description_chars=len(description))
        await self._outbox.flush()
