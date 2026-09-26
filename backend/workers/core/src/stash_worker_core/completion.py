"""What the analyzers (image and document) share once they've described an
item: completing it hands the item on to the embedding stage."""

from stash_shared.log import get_logger
from stash_shared.queue.base import EMBEDDING_JOBS, ProcessingJob

logger = get_logger(__name__)


def embedding_job_for(job: ProcessingJob) -> ProcessingJob:
    """The embedding stage's job for the item `job` is about: its id
    only; the embedding worker reads the text itself."""
    return ProcessingJob(item_id=job.item_id, user_id=job.user_id, item_type=job.item_type)


def log_completion(completed: bool, *, description_chars: int, **fields) -> None:
    """The item's final state transition, shared by the image and document
    analyzers. `fields`: anything else worth logging about the description
    (sizes and counts, never its content)."""
    if completed:
        logger.info(
            "Item completed with generated description",
            item_status="completed",
            description_chars=description_chars,
            next_queue=EMBEDDING_JOBS,
            **fields,
        )
    else:
        # A concurrent duplicate delivery finished it first; see
        # `complete_item`.
        logger.info("Item was already finished; generated description discarded")
