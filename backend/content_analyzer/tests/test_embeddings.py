import uuid

import openai
import pytest
from sqlalchemy import text
from stash_shared.embeddings import EMBEDDING_DIMENSIONS
from stash_shared.queue.base import Delivery, ItemType, ProcessingJob

from content_analyzer.embeddings import EmbeddingHandler
from content_analyzer.items import description_hash, save_embedding
from content_analyzer.sweeper import StaleItemSweeper
from content_analyzer.worker import Worker
from conftest import FakeDeadLetterQueue, FakeJobQueue, fetch_status, insert_item

try:  # the transport library the installed openai SDK builds its errors on
    import httpx2 as httpx
except ImportError:
    import httpx


class _FakeEmbedder:
    def __init__(self, errors: list[Exception] | None = None, on_embed=None):
        self.texts: list[str] = []
        self.errors = list(errors or [])
        self.on_embed = on_embed

    async def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        if self.on_embed is not None:
            await self.on_embed()
        if self.errors:
            raise self.errors.pop(0)
        return [0.5] * EMBEDDING_DIMENSIONS


async def _set_description(engine, item_id, value: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO item_descriptions (item_id, text) VALUES (:id, :text) "
                "ON CONFLICT (item_id) DO UPDATE SET text = excluded.text"
            ),
            {"id": str(item_id), "text": value},
        )


async def _embedding_hash(engine, item_id) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT content_hash FROM item_embeddings WHERE item_id = :id"), {"id": str(item_id)}
        )
        return result.scalar_one_or_none()


def _delivery(item_id, item_type=ItemType.text, delivery_count: int = 1) -> Delivery:
    job = ProcessingJob(item_id=item_id, user_id=uuid.uuid4(), item_type=item_type)
    return Delivery(message_id="1-0", receipt="1-0", delivery_count=delivery_count, raw_payload="{}", job=job)


def _worker(engine, embedder, queue, dead_letters) -> Worker:
    return Worker(
        queue=queue,
        dead_letters=dead_letters,
        engine=engine,
        handler=EmbeddingHandler(embedder=embedder, engine=engine),
        item_type=None,
        manages_item_status=False,
        max_attempts=5,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def dead_letters() -> FakeDeadLetterQueue:
    return FakeDeadLetterQueue()


@pytest.mark.parametrize("item_type", [ItemType.text, ItemType.link, ItemType.image, ItemType.file])
async def test_embeds_current_description_of_finished_item(engine, queue, dead_letters, item_type):
    """Any item type; runs for items that are already `completed`, and
    leaves their status alone."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed", item_type=item_type.value)
    await _set_description(engine, item_id, "our cat Mochi")
    embedder = _FakeEmbedder()

    await _worker(engine, embedder, queue, dead_letters).process_message(_delivery(item_id, item_type))

    assert embedder.texts == ["our cat Mochi"]
    assert await _embedding_hash(engine, item_id) == description_hash("our cat Mochi")
    assert await fetch_status(engine, item_id) == "completed"
    assert len(queue.acked) == 1


async def test_unchanged_text_is_not_embedded_again(engine, queue, dead_letters):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed", item_type="text")
    await _set_description(engine, item_id, "call mom")
    embedder = _FakeEmbedder()
    worker = _worker(engine, embedder, queue, dead_letters)

    await worker.process_message(_delivery(item_id))
    await worker.process_message(_delivery(item_id))

    assert embedder.texts == ["call mom"]
    assert len(queue.acked) == 2


async def test_changed_text_replaces_embedding(engine, queue, dead_letters):
    """E.g. a caption embedded at upload, then caption + generated
    description once analysis is done."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed")
    embedder = _FakeEmbedder()
    worker = _worker(engine, embedder, queue, dead_letters)

    await _set_description(engine, item_id, "our cat")
    await worker.process_message(_delivery(item_id, ItemType.image))
    await _set_description(engine, item_id, "our cat\n\nA grey cat asleep on a sofa.")
    await worker.process_message(_delivery(item_id, ItemType.image))

    assert embedder.texts == ["our cat", "our cat\n\nA grey cat asleep on a sofa."]
    assert await _embedding_hash(engine, item_id) == description_hash("our cat\n\nA grey cat asleep on a sofa.")
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT count(*) FROM item_embeddings"))).scalar_one()
    assert rows == 1


async def test_text_changed_while_embedding_is_not_overwritten_by_stale_vector(engine, queue, dead_letters):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")
    await _set_description(engine, item_id, "our cat")

    async def description_changes_meanwhile():
        await _set_description(engine, item_id, "our cat\n\nA grey cat on a sofa.")

    embedder = _FakeEmbedder(on_embed=description_changes_meanwhile)

    await _worker(engine, embedder, queue, dead_letters).process_message(_delivery(item_id, ItemType.image))

    # The vector for "our cat" was dropped; the newer text's own job will
    # embed it (or the sweeper will).
    assert await _embedding_hash(engine, item_id) is None
    assert len(queue.acked) == 1


async def test_item_without_description_is_skipped(engine, queue, dead_letters):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="failed")
    embedder = _FakeEmbedder()

    await _worker(engine, embedder, queue, dead_letters).process_message(_delivery(item_id, ItemType.image))

    assert embedder.texts == []
    assert len(queue.acked) == 1
    assert dead_letters.letters == []


async def test_save_embedding_for_deleted_item_writes_nothing(engine):
    assert await save_embedding(engine, uuid.uuid4(), embedding="[0.1]", content_hash="x") is False


async def test_outage_is_retried_then_dead_lettered_without_failing_the_item(engine, queue, dead_letters):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed", item_type="text")
    await _set_description(engine, item_id, "call mom")
    embedder = _FakeEmbedder(errors=[ConnectionError("down")] * 5)
    worker = _worker(engine, embedder, queue, dead_letters)

    for attempt in range(1, 5):
        await worker.process_message(_delivery(item_id, delivery_count=attempt))
    assert len(queue.retried) == 4

    await worker.process_message(_delivery(item_id, delivery_count=5))

    assert len(dead_letters.letters) == 1
    assert len(queue.acked) == 1
    # A failed embedding doesn't make a saved item "failed".
    assert await fetch_status(engine, item_id) == "completed"


async def test_rejected_input_is_dead_lettered_immediately(engine, queue, dead_letters):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed", item_type="text")
    await _set_description(engine, item_id, "call mom")
    request = httpx.Request("POST", "https://api.openai.test/v1/embeddings")
    rejection = openai.BadRequestError("bad input", response=httpx.Response(400, request=request), body=None)

    await _worker(engine, _FakeEmbedder(errors=[rejection]), queue, dead_letters).process_message(_delivery(item_id))

    assert queue.retried == []
    assert len(dead_letters.letters) == 1
    assert await fetch_status(engine, item_id) == "completed"


# ---- sweeper safety net ----


def _sweeper(engine, embedding_queue) -> StaleItemSweeper:
    return StaleItemSweeper(
        thumbnail_queue=FakeJobQueue(),
        analysis_queue=FakeJobQueue(),
        document_queue=FakeJobQueue(),
        embedding_queue=embedding_queue,
        engine=engine,
        stale_after_seconds=1800,
        max_requeues=3,
        interval_seconds=60,
        embedding_settle_seconds=600,
    )


async def test_sweeper_republishes_missing_and_stale_embeddings(engine):
    missing, stale, current, recent = (uuid.uuid4() for _ in range(4))
    for item_id in (missing, stale, current):
        await insert_item(engine, item_id, status="completed", item_type="text", age_seconds=3600)
        await _set_description(engine, item_id, f"note {item_id}")
    # Changed a moment ago: its embedding event may still be in flight.
    await insert_item(engine, recent, status="completed", item_type="text", age_seconds=5)
    await _set_description(engine, recent, "brand new note")
    await save_embedding(engine, current, embedding="[0.1]", content_hash=description_hash(f"note {current}"))
    await save_embedding(engine, stale, embedding="[0.1]", content_hash=description_hash(f"note {stale}"))
    await _set_description(engine, stale, "edited later")
    embedding_queue = FakeJobQueue()

    await _sweeper(engine, embedding_queue).sweep_once()

    assert {job.item_id for job in embedding_queue.published} == {missing, stale}
    assert all(job.item_type == ItemType.text for job in embedding_queue.published)
