from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.queue.base import ItemType as QueueItemType

from app.items.models import Item, ItemType, TextContent
from conftest import FakeJobQueue
from helpers import register_and_login


async def test_create_text_item_persists_and_associates_with_user(client: AsyncClient, session: AsyncSession):
    user_id, token = await register_and_login(client)

    response = await client.post(
        "/items/text", json={"text": "hello world"}, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"

    item = await session.get(Item, UUID(body["id"]))
    assert item is not None
    assert item.type == ItemType.text
    assert str(item.user_id) == user_id

    text_content = await session.execute(select(TextContent).where(TextContent.item_id == item.id))
    assert text_content.scalar_one().text == "hello world"


async def test_create_text_item_rejects_empty_text(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.post("/items/text", json={"text": ""}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 422


async def test_create_text_item_rejects_whitespace_only_text(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.post("/items/text", json={"text": "   "}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 422


async def test_create_text_item_rejects_missing_token(client: AsyncClient):
    response = await client.post("/items/text", json={"text": "hello"})

    assert response.status_code == 401


async def test_create_text_item_publishes_processing_job(client: AsyncClient, queue: FakeJobQueue):
    user_id, token = await register_and_login(client)

    response = await client.post(
        "/items/text", json={"text": "hello world"}, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 202
    assert len(queue.published) == 1
    job = queue.published[0]
    assert str(job.item_id) == response.json()["id"]
    assert str(job.user_id) == user_id
    assert job.item_type == QueueItemType.text


async def test_create_text_item_marked_failed_when_enqueue_fails(
    client: AsyncClient, session: AsyncSession, queue: FakeJobQueue
):
    queue.fail_publish = True
    _, token = await register_and_login(client)

    response = await client.post(
        "/items/text", json={"text": "hello world"}, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 202
    assert response.json()["status"] == "failed"

    item = await session.get(Item, UUID(response.json()["id"]))
    assert item.status == "failed"
    assert queue.published == []
