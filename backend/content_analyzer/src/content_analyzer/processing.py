from stash_shared.queue.base import ProcessingJob


async def process_item(job: ProcessingJob) -> None:
    """Placeholder for real content analysis (descriptions, tags,
    embeddings). Deliberately isolated so a later task can replace this one
    function without touching the surrounding retry/status-transition
    machinery in `Worker`."""
    return None
