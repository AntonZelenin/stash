from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Item, ItemType, TextContent


async def _register_and_login(client: AsyncClient, email: str = "alice@example.com", password: str = "correct-horse"):
    register_response = await client.post("/users", json={"email": email, "password": password})
    login_response = await client.post("/login", json={"email": email, "password": password})
    return register_response.json()["id"], login_response.json()["access_token"]


async def test_create_text_item_persists_and_associates_with_user(client: AsyncClient, session: AsyncSession):
    user_id, token = await _register_and_login(client)

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
    _, token = await _register_and_login(client)

    response = await client.post("/items/text", json={"text": ""}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 422


async def test_create_text_item_rejects_whitespace_only_text(client: AsyncClient):
    _, token = await _register_and_login(client)

    response = await client.post("/items/text", json={"text": "   "}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 422


async def test_create_text_item_rejects_missing_token(client: AsyncClient):
    response = await client.post("/items/text", json={"text": "hello"})

    assert response.status_code == 401
