from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Item, ItemType
from conftest import FakeJobQueue
from helpers import register_and_login


@pytest.mark.parametrize(
    "text",
    [
        "http://example.com",
        "https://example.com",
        "https://example.com/path?query=1#fragment",
        "HTTPS://Example.com",
        "https://example.com:8080/a/b",
    ],
)
async def test_create_text_item_classifies_bare_url_as_link(client: AsyncClient, session: AsyncSession, text: str):
    _, token = await register_and_login(client)

    response = await client.post("/items/text", json={"text": text}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 202
    item = await session.get(Item, UUID(response.json()["id"]))
    assert item.type == ItemType.link


@pytest.mark.parametrize(
    "text",
    [
        "just a note",
        "check this out: https://example.com",
        "https://example.com is a great site",
        "ftp://example.com",
        "javascript:alert(1)",
        "example.com",
        "http://",
    ],
)
async def test_create_text_item_classifies_everything_else_as_text(
    client: AsyncClient, session: AsyncSession, text: str
):
    _, token = await register_and_login(client)

    response = await client.post("/items/text", json={"text": text}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 202
    item = await session.get(Item, UUID(response.json()["id"]))
    assert item.type == ItemType.text


async def test_create_text_item_link_type_is_returned_by_list_items(client: AsyncClient):
    _, token = await register_and_login(client)
    await client.post(
        "/items/text", json={"text": "https://example.com"}, headers={"Authorization": f"Bearer {token}"}
    )

    response = await client.get("/items", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["items"][0]["type"] == "link"


async def test_create_text_item_link_does_not_publish_processing_job(client: AsyncClient, queue: FakeJobQueue):
    _, token = await register_and_login(client)

    response = await client.post(
        "/items/text", json={"text": "https://example.com"}, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 202
    assert queue.published == []
