from datetime import datetime, timedelta, timezone
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Item
from helpers import register_and_login

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360000000020001e221bc330000000049454e"
    "44ae426082"
)


async def _set_created_at(session: AsyncSession, item_id: str, created_at: datetime) -> None:
    """Test items are created back-to-back through the API, so their
    DB-assigned `created_at` values can collide at whatever resolution the
    test DB offers. Pinning them directly gives each test a known, distinct
    order to assert against."""
    item = await session.get(Item, UUID(item_id))
    assert item is not None
    item.created_at = created_at
    await session.commit()


async def _create_text_item(client: AsyncClient, token: str, text: str) -> str:
    response = await client.post("/items/text", json={"text": text}, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 202
    return response.json()["id"]


async def test_list_items_orders_by_created_at_desc(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)

    oldest = await _create_text_item(client, token, "oldest")
    middle = await _create_text_item(client, token, "middle")
    newest = await _create_text_item(client, token, "newest")
    await _set_created_at(session, oldest, base)
    await _set_created_at(session, middle, base + timedelta(minutes=1))
    await _set_created_at(session, newest, base + timedelta(minutes=2))

    response = await client.get("/items", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [newest, middle, oldest]
    assert body["next_cursor"] is None


async def test_list_items_breaks_created_at_ties_by_id_desc(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    same_time = datetime(2024, 1, 1, tzinfo=timezone.utc)

    first = await _create_text_item(client, token, "first")
    second = await _create_text_item(client, token, "second")
    await _set_created_at(session, first, same_time)
    await _set_created_at(session, second, same_time)

    response = await client.get("/items", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["items"]]
    assert ids == sorted([first, second], reverse=True)


async def test_list_items_paginates_with_cursor(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)

    ids = [await _create_text_item(client, token, f"item {i}") for i in range(3)]
    for i, item_id in enumerate(ids):
        await _set_created_at(session, item_id, base + timedelta(minutes=i))
    newest, middle, oldest = reversed(ids)

    first_page = await client.get("/items?limit=2", headers={"Authorization": f"Bearer {token}"})
    assert first_page.status_code == 200
    first_body = first_page.json()
    assert [item["id"] for item in first_body["items"]] == [newest, middle]
    assert first_body["next_cursor"] is not None

    second_page = await client.get(
        f"/items?limit=2&cursor={first_body['next_cursor']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert second_page.status_code == 200
    second_body = second_page.json()
    assert [item["id"] for item in second_body["items"]] == [oldest]
    assert second_body["next_cursor"] is None


async def test_list_items_next_cursor_null_when_exactly_limit_items(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)

    ids = [await _create_text_item(client, token, f"item {i}") for i in range(2)]
    for i, item_id in enumerate(ids):
        await _set_created_at(session, item_id, base + timedelta(minutes=i))

    response = await client.get("/items?limit=2", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["next_cursor"] is None


async def test_list_items_rejects_invalid_cursor(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.get("/items?cursor=not-a-valid-cursor", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 422


async def test_list_items_rejects_missing_token(client: AsyncClient):
    response = await client.get("/items")

    assert response.status_code == 401


async def test_list_items_only_returns_current_users_items(client: AsyncClient):
    _, token_a = await register_and_login(client, email="alice@example.com")
    _, token_b = await register_and_login(client, email="bob@example.com")

    await _create_text_item(client, token_a, "alice's note")
    await _create_text_item(client, token_b, "bob's note")

    response = await client.get("/items", headers={"Authorization": f"Bearer {token_a}"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["text"] == "alice's note"


async def test_list_items_text_item_has_no_download_url(client: AsyncClient):
    _, token = await register_and_login(client)
    await _create_text_item(client, token, "just text")

    response = await client.get("/items", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["type"] == "text"
    assert item["text"] == "just text"
    assert item["download_url"] is None


async def test_list_items_image_item_has_presigned_download_url(client: AsyncClient):
    _, token = await register_and_login(client)
    create_response = await client.post(
        "/items/image",
        files={"file": ("photo.png", _PNG_BYTES, "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_response.status_code == 202

    response = await client.get("/items", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["type"] == "image"
    assert item["text"] is None
    assert item["download_url"] is not None
    assert item["download_url"].startswith("https://fake-storage.test/images/")
