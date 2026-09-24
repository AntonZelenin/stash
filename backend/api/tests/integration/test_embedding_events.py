from uuid import UUID

from httpx import AsyncClient
from stash_shared.queue.base import ItemType as QueueItemType

from conftest import FakeJobQueue
from helpers import register_and_login

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


async def test_captioned_image_is_sent_for_embedding_right_away(client: AsyncClient, embedding_queue: FakeJobQueue):
    _, token = await register_and_login(client)

    await client.post(
        "/items/image",
        files={"file": ("photo.png", _PNG_BYTES, "image/png")},
        data={"text": "our cat"},
        headers=_auth(token),
    )

    [job] = embedding_queue.published
    assert job.item_type == QueueItemType.image


async def test_uncaptioned_uploads_wait_for_analysis(client: AsyncClient, embedding_queue: FakeJobQueue):
    """No searchable text yet: the content analyzers publish once they've
    written the description."""
    _, token = await register_and_login(client)

    await client.post("/items/image", files={"file": ("photo.png", _PNG_BYTES, "image/png")}, headers=_auth(token))
    await client.post("/items/file", files={"file": ("notes.txt", b"hello", "text/plain")}, headers=_auth(token))

    assert embedding_queue.published == []


async def test_captioned_unanalyzable_file_is_sent_for_embedding(client: AsyncClient, embedding_queue: FakeJobQueue):
    _, token = await register_and_login(client)

    await client.post(
        "/items/file",
        files={"file": ("setup.exe", b"MZ\x90\x00", "application/octet-stream")},
        data={"text": "printer driver"},
        headers=_auth(token),
    )

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
