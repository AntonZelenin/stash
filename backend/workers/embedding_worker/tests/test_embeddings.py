import uuid

import openai
import pytest
from sqlalchemy import text
from stash_shared.embeddings import EMBEDDING_DIMENSIONS, to_pgvector
from stash_shared.queue.base import Delivery, ItemType, ProcessingJob
from stash_worker_core.testing import FakeDeadLetterQueue, FakeJobQueue, fetch_status, insert_item
from stash_worker_core.worker import Worker

from embedding_worker.handler import EmbeddingHandler
from embedding_worker.items import EmbeddedChunk, replace_chunks

try:  # the transport library the installed openai SDK builds its errors on
    import httpx2 as httpx
except ImportError:
    import httpx


class _FakeEmbedder:
    """Records each request's inputs. Each text's vector encodes the text's
    length, so tests can tell which vector was stored for which chunk."""

    def __init__(self, errors: list[Exception] | None = None, on_embed=None):
        self.requests: list[list[str]] = []
        self.errors = list(errors or [])
        self.on_embed = on_embed

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.requests.append(list(texts))
        if self.on_embed is not None:
            await self.on_embed()
        if self.errors:
            raise self.errors.pop(0)
        return [_vector_for(text) for text in texts]


def _vector_for(value: str) -> list[float]:
    return [float(len(value))] + [0.0] * (EMBEDDING_DIMENSIONS - 1)


async def _set_description(engine, item_id, value: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO item_descriptions (item_id, text) VALUES (:id, :text) "
                "ON CONFLICT (item_id) DO UPDATE SET text = excluded.text"
            ),
            {"id": str(item_id), "text": value},
        )


async def _stored_chunks(engine, item_id) -> list[str]:
    """The texts of the item's stored chunks, in order. (Their vectors
    can't be read back: SQLite turns `CAST(... AS vector)` into a number.
    `saved_chunks` shows which vector went with which text.)"""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT text FROM item_search_chunks WHERE item_id = :id ORDER BY position"),
            {"id": str(item_id)},
        )
        return list(result.scalars())


@pytest.fixture
def saved_chunks(monkeypatch) -> list[list[EmbeddedChunk]]:
    """Every list of chunks the handler stores, as it passes them on."""
    from embedding_worker import handler

    calls: list[list[EmbeddedChunk]] = []

    async def recording_replace_chunks(engine, item_id, chunks):
        calls.append(chunks)
        return await replace_chunks(engine, item_id, chunks)

    monkeypatch.setattr(handler, "replace_chunks", recording_replace_chunks)
    return calls


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

    assert embedder.requests == [["our cat Mochi"]]
    assert await _stored_chunks(engine, item_id) == ["our cat Mochi"]
    assert await fetch_status(engine, item_id) == "completed"
    assert len(queue.acked) == 1


async def test_note_is_one_chunk_whatever_its_lines(engine, queue, dead_letters):
    item_id = uuid.uuid4()
    note = "shopping\nmilk\neggs"
    await insert_item(engine, item_id, status="completed", item_type="text", caption=note)
    await _set_description(engine, item_id, note)
    embedder = _FakeEmbedder()

    await _worker(engine, embedder, queue, dead_letters).process_message(_delivery(item_id))

    assert embedder.requests == [[note]]


async def test_image_chunks_are_embedded_in_one_request_and_stored_one_row_each(
    engine, queue, dead_letters, saved_chunks
):
    """The caption is one chunk, then each generated chunk (one per line);
    each row keeps the vector made from its own text."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed", caption="my wallpaper")
    await _set_description(
        engine,
        item_id,
        "my wallpaper\n\nanime woman, girl, female\ncyberpunk city, futuristic urban environment\n"
        "cybernetic leg, mechanical cables",
    )
    embedder = _FakeEmbedder()

    await _worker(engine, embedder, queue, dead_letters).process_message(_delivery(item_id, ItemType.image))

    chunks = [
        "my wallpaper",
        "anime woman, girl, female",
        "cyberpunk city, futuristic urban environment",
        "cybernetic leg, mechanical cables",
    ]
    assert embedder.requests == [chunks]
    assert await _stored_chunks(engine, item_id) == chunks
    [saved] = saved_chunks
    assert saved == [EmbeddedChunk(text=chunk, embedding=to_pgvector(_vector_for(chunk))) for chunk in chunks]


async def test_unchanged_text_is_not_embedded_again(engine, queue, dead_letters):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed")
    await _set_description(engine, item_id, "grey cat\nsofa")
    embedder = _FakeEmbedder()
    worker = _worker(engine, embedder, queue, dead_letters)

    await worker.process_message(_delivery(item_id, ItemType.image))
    await worker.process_message(_delivery(item_id, ItemType.image))

    assert embedder.requests == [["grey cat", "sofa"]]
    assert len(queue.acked) == 2


async def test_changed_text_replaces_all_chunks(engine, queue, dead_letters):
    """E.g. a caption embedded at upload, then caption + generated chunks
    once analysis is done, then reprocessed into other chunks: each time
    the old rows are replaced, never added to."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed", caption="our cat")
    embedder = _FakeEmbedder()
    worker = _worker(engine, embedder, queue, dead_letters)

    await _set_description(engine, item_id, "our cat")
    await worker.process_message(_delivery(item_id, ItemType.image))
    await _set_description(engine, item_id, "our cat\n\ngrey cat, kitten\nsofa, living room\nsleeping")
    await worker.process_message(_delivery(item_id, ItemType.image))
    await _set_description(engine, item_id, "our cat\n\ngrey cat asleep on a sofa")
    await worker.process_message(_delivery(item_id, ItemType.image))

    assert embedder.requests == [
        ["our cat"],
        ["our cat", "grey cat, kitten", "sofa, living room", "sleeping"],
        ["our cat", "grey cat asleep on a sofa"],
    ]
    assert await _stored_chunks(engine, item_id) == ["our cat", "grey cat asleep on a sofa"]
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT count(*) FROM item_search_chunks"))).scalar_one()
    assert rows == 2


async def test_text_changed_while_embedding_is_not_overwritten_by_stale_vectors(engine, queue, dead_letters):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")
    await _set_description(engine, item_id, "our cat")

    async def description_changes_meanwhile():
        await _set_description(engine, item_id, "grey cat\nsofa")

    embedder = _FakeEmbedder(on_embed=description_changes_meanwhile)

    await _worker(engine, embedder, queue, dead_letters).process_message(_delivery(item_id, ItemType.image))

    # The vectors for "our cat" were dropped; the newer text's own job will
    # embed it.
    assert await _stored_chunks(engine, item_id) == []
    assert len(queue.acked) == 1


async def test_item_without_description_is_skipped(engine, queue, dead_letters):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="failed")
    embedder = _FakeEmbedder()

    await _worker(engine, embedder, queue, dead_letters).process_message(_delivery(item_id, ItemType.image))

    assert embedder.requests == []
    assert len(queue.acked) == 1
    assert dead_letters.letters == []


async def test_replace_chunks_for_deleted_item_writes_nothing(engine):
    assert await replace_chunks(engine, uuid.uuid4(), [EmbeddedChunk(text="cat", embedding="[0.1]")]) is False


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


async def test_openai_embedder_sends_every_text_in_one_request(monkeypatch):
    """One request for all of an item's chunks; each vector goes back to
    its own text by the `index` OpenAI returns, whatever the response's
    order."""
    from types import SimpleNamespace

    from stash_shared.embeddings import OpenAIEmbedder

    embedder = OpenAIEmbedder(api_key="test")
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        data = [SimpleNamespace(index=i, embedding=[float(i)]) for i in range(len(kwargs["input"]))]
        return SimpleNamespace(data=list(reversed(data)))

    monkeypatch.setattr(embedder._client.embeddings, "create", create)

    vectors = await embedder.embed_many(["girl", "city", "cables"])

    assert [request["input"] for request in requests] == [["girl", "city", "cables"]]
    assert vectors == [[0.0], [1.0], [2.0]]
