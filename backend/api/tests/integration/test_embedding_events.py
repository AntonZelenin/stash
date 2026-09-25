from uuid import UUID

from httpx import AsyncClient
from stash_shared.queue.base import ItemType as QueueItemType

from conftest import FakeJobQueue, FakeObjectStorage
from helpers import register_and_login, upload_file, upload_image

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5a1e8b10000000049454e44ae426082"
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_text_item_is_sent_for_embedding(client: AsyncClient, embedding_queue: FakeJobQueue):
    user_id, token = await register_and_login(client)

    response = await client.post("/items/text", json={"text": "call mom"}, headers=_auth(token))

    [job] = embedding_queue.published
    assert job.item_id == UUID(response.json()["id"])
    assert str(job.user_id) == user_id
    assert job.item_type == QueueItemType.text
    # Only the item id: the worker reads the text from the database.
    assert job.image is None and job.file is None


async def test_link_item_is_sent_for_embedding(client: AsyncClient, embedding_queue: FakeJobQueue):
    _, token = await register_and_login(client)

    await client.post("/items/text", json={"text": "https://example.com"}, headers=_auth(token))

    [job] = embedding_queue.published
    assert job.item_type == QueueItemType.link


async def test_captioned_image_is_sent_for_embedding_right_away(client: AsyncClient, storage: FakeObjectStorage, embedding_queue: FakeJobQueue):
    _, token = await register_and_login(client)

    await upload_image(client, storage, token, _PNG_BYTES, text="our cat")

    [job] = embedding_queue.published
    assert job.item_type == QueueItemType.image


async def test_uncaptioned_uploads_wait_for_analysis(client: AsyncClient, storage: FakeObjectStorage, embedding_queue: FakeJobQueue):
    """No searchable text yet: the content analyzers publish once they've
    written the description."""
    _, token = await register_and_login(client)

    await upload_image(client, storage, token, _PNG_BYTES)
    await upload_file(client, storage, token, "notes.txt", b"hello")

    assert embedding_queue.published == []


async def test_captioned_unanalyzable_file_is_sent_for_embedding(client: AsyncClient, storage: FakeObjectStorage, embedding_queue: FakeJobQueue):
    _, token = await register_and_login(client)

    await upload_file(client, storage, token, "setup.exe", b"MZ\x90\x00", text="printer driver")

    [job] = embedding_queue.published
    assert job.item_type == QueueItemType.file


async def test_item_is_saved_even_if_embedding_event_cannot_be_published(
    client: AsyncClient, embedding_queue: FakeJobQueue
):
    embedding_queue.fail_publish = True
    _, token = await register_and_login(client)

    response = await client.post("/items/text", json={"text": "call mom"}, headers=_auth(token))

    assert response.status_code == 202
    assert response.json()["status"] == "completed"
    listed = (await client.get("/items", headers=_auth(token))).json()["items"]
    assert [item["text"] for item in listed] == ["call mom"]
    assert embedding_queue.published == []

    # Left in the outbox: the next request that flushes publishes it, with
    # its own event.
    embedding_queue.fail_publish = False
    second = await client.post("/items/text", json={"text": "buy milk"}, headers=_auth(token))

    assert [job.item_id for job in embedding_queue.published] == [
        UUID(response.json()["id"]),
        UUID(second.json()["id"]),
    ]
