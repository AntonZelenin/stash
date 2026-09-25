"""The API's writes and the jobs they trigger go through the transactional
outbox (`stash_shared.outbox`): committed together, or not at all."""

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.queue.base import DOCUMENT_ANALYSIS_JOBS, EMBEDDING_JOBS, THUMBNAIL_JOBS

from app.items import services
from app.items.models import Item
from app.outbox import outbox_events
from conftest import FakeJobQueue, FakeObjectStorage, FakeQueues
from helpers import register_and_login, upload_file, upload_image

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360000000020001e221bc330000000049454e"
    "44ae426082"
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _events(session: AsyncSession) -> list:
    result = await session.execute(select(outbox_events).order_by(outbox_events.c.created_at))
    return result.all()


async def _item_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Item))).scalar_one()


async def test_item_and_its_jobs_are_committed_together_and_published(
    client: AsyncClient, storage: FakeObjectStorage, session: AsyncSession, queue: FakeJobQueue, embedding_queue: FakeJobQueue
):
    _, token = await register_and_login(client)

    response = await upload_image(client, storage, token, _PNG_BYTES, text="our cat")

    item_id = UUID(response.json()["id"])
    assert await session.get(Item, item_id) is not None
    events = await _events(session)
    assert sorted(event.queue for event in events) == sorted([THUMBNAIL_JOBS, EMBEDDING_JOBS])
    assert all(event.published_at is not None for event in events)
    assert [job.item_id for job in queue.published] == [item_id]
    assert [job.item_id for job in embedding_queue.published] == [item_id]


async def test_item_is_not_committed_if_its_job_cannot_be_added(
    client: AsyncClient, storage: FakeObjectStorage, session: AsyncSession, queues: FakeQueues, monkeypatch: pytest.MonkeyPatch
):
    """The item is already written (flushed) in the transaction when adding
    its job fails: the whole transaction rolls back."""
    _, token = await register_and_login(client)

    async def failing_add_event(*_args, **_kwargs):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(services, "add_event", failing_add_event)

    with pytest.raises(RuntimeError, match="outbox insert failed"):
        await upload_image(client, storage, token, _PNG_BYTES)

    assert await _item_count(session) == 0
    assert await _events(session) == []
    assert all(queue.published == [] for queue in queues.by_name.values())


async def test_job_is_not_committed_if_the_item_is_not(
    client: AsyncClient, session: AsyncSession, queues: FakeQueues, monkeypatch: pytest.MonkeyPatch
):
    """The job is already in the transaction when the commit fails: neither
    is committed, and nothing is published."""
    _, token = await register_and_login(client)

    async def failing_commit(self):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            await client.post("/items/text", json={"text": "call mom"}, headers=_auth(token))
    finally:
        monkeypatch.undo()

    assert await _item_count(session) == 0
    assert await _events(session) == []
    assert all(queue.published == [] for queue in queues.by_name.values())


async def test_unpublished_jobs_of_every_queue_are_published_by_the_next_flush(
    client: AsyncClient, storage: FakeObjectStorage,
    session: AsyncSession,
    queue: FakeJobQueue,
    document_queue: FakeJobQueue,
    embedding_queue: FakeJobQueue,
):
    """A flush publishes every unpublished event, not just the request's
    own: here, jobs of three earlier requests left behind by an outage."""
    _, token = await register_and_login(client)
    for fake in (queue, document_queue, embedding_queue):
        fake.fail_publish = True

    image = await upload_image(client, storage, token, _PNG_BYTES)
    document = await upload_file(client, storage, token, "notes.txt", b"hello")
    note = await client.post("/items/text", json={"text": "call mom"}, headers=_auth(token))
    assert len([event for event in await _events(session) if event.published_at is None]) == 3

    for fake in (queue, document_queue, embedding_queue):
        fake.fail_publish = False
    latest = await client.post("/items/text", json={"text": "buy milk"}, headers=_auth(token))

    assert [job.item_id for job in queue.published] == [UUID(image.json()["id"])]
    assert [job.item_id for job in document_queue.published] == [UUID(document.json()["id"])]
    assert [job.item_id for job in embedding_queue.published] == [
        UUID(note.json()["id"]),
        UUID(latest.json()["id"]),
    ]
    session.expire_all()
    assert all(event.published_at is not None for event in await _events(session))


async def test_one_unreachable_queue_does_not_hold_up_the_others(
    client: AsyncClient, storage: FakeObjectStorage, session: AsyncSession, queue: FakeJobQueue, embedding_queue: FakeJobQueue
):
    _, token = await register_and_login(client)
    queue.fail_publish = True

    response = await upload_image(client, storage, token, _PNG_BYTES, text="our cat")

    item_id = UUID(response.json()["id"])
    assert queue.published == []
    assert [job.item_id for job in embedding_queue.published] == [item_id]
    unpublished = [event.queue for event in await _events(session) if event.published_at is None]
    assert unpublished == [THUMBNAIL_JOBS]


async def test_edit_and_its_embedding_job_are_committed_together(
    client: AsyncClient, session: AsyncSession, embedding_queue: FakeJobQueue
):
    _, token = await register_and_login(client)
    created = await client.post("/items/text", json={"text": "call mom"}, headers=_auth(token))
    item_id = created.json()["id"]

    await client.patch(f"/items/{item_id}", json={"text": "call dad"}, headers=_auth(token))

    assert [job.item_id for job in embedding_queue.published] == [UUID(item_id), UUID(item_id)]
    assert [event.queue for event in await _events(session)] == [EMBEDDING_JOBS, EMBEDDING_JOBS]


async def test_file_that_needs_no_analysis_gets_no_document_job(
    client: AsyncClient, storage: FakeObjectStorage, session: AsyncSession, document_queue: FakeJobQueue
):
    _, token = await register_and_login(client)

    await upload_file(client, storage, token, "setup.exe", b"MZ\x90\x00")

    assert document_queue.published == []
    assert DOCUMENT_ANALYSIS_JOBS not in [event.queue for event in await _events(session)]
