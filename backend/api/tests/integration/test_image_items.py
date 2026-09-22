from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.queue.base import ItemType as QueueItemType

from app.items.models import ImageMetadata, Item, ItemType
from conftest import FakeJobQueue, FakeObjectStorage
from helpers import register_and_login

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360000000020001e221bc330000000049454e"
    "44ae426082"
)


async def test_create_image_item_persists_and_associates_with_user(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client)

    response = await client.post(
        "/items/image",
        files={"file": ("photo.png", _PNG_BYTES, "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"

    item = await session.get(Item, UUID(body["id"]))
    assert item is not None
    assert item.type == ItemType.image
    assert str(item.user_id) == user_id

    image = await session.get(ImageMetadata, item.id)
    assert image is not None
    assert image.content_type == "image/png"
    assert image.size_bytes == len(_PNG_BYTES)
    assert image.storage_key.endswith(".png")

    uploaded_data, uploaded_content_type = storage.uploads[image.storage_key]
    assert uploaded_data == _PNG_BYTES
    assert uploaded_content_type == "image/png"


async def test_create_image_item_rejects_empty_file(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.post(
        "/items/image",
        files={"file": ("empty.png", b"", "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422


async def test_create_image_item_rejects_non_image_content(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.post(
        "/items/image",
        files={"file": ("not-an-image.txt", b"just some text", "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422


async def test_create_image_item_rejects_oversized_file(client: AsyncClient):
    _, token = await register_and_login(client)

    oversized = _PNG_BYTES[:8] + b"\x00" * (10 * 1024 * 1024 + 1)

    response = await client.post(
        "/items/image",
        files={"file": ("big.png", oversized, "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422


async def test_create_image_item_rejects_missing_token(client: AsyncClient):
    response = await client.post(
        "/items/image",
        files={"file": ("photo.png", _PNG_BYTES, "image/png")},
    )

    assert response.status_code == 401


async def test_create_image_item_publishes_processing_job(client: AsyncClient, queue: FakeJobQueue):
    user_id, token = await register_and_login(client)

    response = await client.post(
        "/items/image",
        files={"file": ("photo.png", _PNG_BYTES, "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    assert len(queue.published) == 1
    job = queue.published[0]
    assert str(job.item_id) == response.json()["id"]
    assert str(job.user_id) == user_id
    assert job.item_type == QueueItemType.image
