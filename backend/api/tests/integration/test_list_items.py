from datetime import datetime, timedelta, timezone
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Item
from conftest import FakeObjectStorage
from helpers import register_and_login, upload_file, upload_image

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


async def test_list_items_image_item_has_presigned_download_url(client: AsyncClient, storage: FakeObjectStorage):
    user_id, token = await register_and_login(client)
    create_response = await upload_image(client, storage, token, _PNG_BYTES)
    assert create_response.status_code == 202

    response = await client.get("/items", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["type"] == "image"
    assert item["text"] is None
    assert item["download_url"] is not None
    assert item["download_url"].startswith(f"https://fake-storage.test/users/{user_id}/images/")


async def test_list_items_filters_by_created_at_range(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    before = await _create_text_item(client, token, "2024")
    first = await _create_text_item(client, token, "start of 2025")
    last = await _create_text_item(client, token, "end of 2025")
    after = await _create_text_item(client, token, "2026")
    await _set_created_at(session, before, datetime(2024, 12, 31, 23, 59, tzinfo=timezone.utc))
    await _set_created_at(session, first, datetime(2025, 1, 1, tzinfo=timezone.utc))
    await _set_created_at(session, last, datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc))
    await _set_created_at(session, after, datetime(2026, 1, 1, tzinfo=timezone.utc))

    response = await client.get(
        "/items",
        params={"created_from": "2025-01-01T00:00:00Z", "created_before": "2026-01-01T00:00:00Z"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [last, first]


async def test_list_items_date_range_combines_with_favorites(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    auth = {"Authorization": f"Bearer {token}"}
    favorite_2025 = await _create_text_item(client, token, "favorite 2025")
    plain_2025 = await _create_text_item(client, token, "plain 2025")
    favorite_2024 = await _create_text_item(client, token, "favorite 2024")
    await _set_created_at(session, favorite_2025, datetime(2025, 6, 1, tzinfo=timezone.utc))
    await _set_created_at(session, plain_2025, datetime(2025, 6, 2, tzinfo=timezone.utc))
    await _set_created_at(session, favorite_2024, datetime(2024, 6, 1, tzinfo=timezone.utc))
    for item_id in (favorite_2025, favorite_2024):
        await client.put(f"/items/{item_id}/favorite", headers=auth)

    response = await client.get(
        "/items",
        params={"favorite": "true", "created_from": "2025-01-01T00:00:00Z", "created_before": "2026-01-01T00:00:00Z"},
        headers=auth,
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [favorite_2025]


async def test_list_items_rejects_dates_without_time_zone(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.get(
        "/items", params={"created_from": "2025-01-01T00:00:00"}, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 422


async def test_saved_years_gives_first_and_last_save_of_each_year(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    saved = {
        "early 2023": datetime(2023, 2, 1, tzinfo=timezone.utc),
        "late 2023": datetime(2023, 11, 5, tzinfo=timezone.utc),
        "mid 2023": datetime(2023, 6, 1, tzinfo=timezone.utc),
        "2025": datetime(2025, 3, 3, 12, 30, tzinfo=timezone.utc),
    }
    for text, created_at in saved.items():
        await _set_created_at(session, await _create_text_item(client, token, text), created_at)

    response = await client.get("/items/years", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    years = [
        (datetime.fromisoformat(year["first_saved_at"]), datetime.fromisoformat(year["last_saved_at"]))
        for year in response.json()["years"]
    ]
    # SQLite hands timestamps back without their offset.
    naive = lambda time: time.replace(tzinfo=None)  # noqa: E731
    assert [(naive(first), naive(last)) for first, last in years] == [
        (datetime(2023, 2, 1), datetime(2023, 11, 5)),
        (datetime(2025, 3, 3, 12, 30), datetime(2025, 3, 3, 12, 30)),
    ]


async def test_saved_years_only_covers_the_users_own_items(client: AsyncClient):
    _, alice = await register_and_login(client)
    _, bob = await register_and_login(client, email="bob@example.com")
    await _create_text_item(client, alice, "alice's note")

    response = await client.get("/items/years", headers={"Authorization": f"Bearer {bob}"})

    assert response.status_code == 200
    assert response.json() == {"years": []}
    assert (await client.get("/items/years")).status_code == 401


async def _dated_items(client: AsyncClient, session: AsyncSession, token: str, count: int) -> list[str]:
    """`count` notes saved a minute apart, oldest first."""
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    ids = [await _create_text_item(client, token, f"item {i}") for i in range(count)]
    for i, item_id in enumerate(ids):
        await _set_created_at(session, item_id, base + timedelta(minutes=i))
    return ids


async def test_list_items_sorts_oldest_first_and_paginates(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    auth = {"Authorization": f"Bearer {token}"}
    oldest, middle, newest = await _dated_items(client, session, token, 3)

    first_page = (await client.get("/items", params={"sort": "oldest", "limit": 2}, headers=auth)).json()
    assert [item["id"] for item in first_page["items"]] == [oldest, middle]

    second_page = await client.get(
        "/items", params={"sort": "oldest", "limit": 2, "cursor": first_page["next_cursor"]}, headers=auth
    )
    assert second_page.status_code == 200
    assert [item["id"] for item in second_page.json()["items"]] == [newest]
    assert second_page.json()["next_cursor"] is None


async def test_list_items_newest_is_the_default_sort(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    auth = {"Authorization": f"Bearer {token}"}
    oldest, middle, newest = await _dated_items(client, session, token, 3)

    default = (await client.get("/items", headers=auth)).json()["items"]
    explicit = (await client.get("/items", params={"sort": "newest"}, headers=auth)).json()["items"]

    assert [item["id"] for item in default] == [item["id"] for item in explicit] == [newest, middle, oldest]


async def test_list_items_rejects_a_cursor_from_another_order(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    auth = {"Authorization": f"Bearer {token}"}
    await _dated_items(client, session, token, 3)
    newest_cursor = (await client.get("/items", params={"limit": 1}, headers=auth)).json()["next_cursor"]
    oldest_cursor = (await client.get("/items", params={"limit": 1, "sort": "oldest"}, headers=auth)).json()[
        "next_cursor"
    ]

    for sort, cursor in (("oldest", newest_cursor), ("newest", oldest_cursor), ("random", newest_cursor)):
        response = await client.get("/items", params={"sort": sort, "cursor": cursor}, headers=auth)
        assert response.status_code == 422, sort


async def test_list_items_random_only_shuffles_the_users_own_matching_items(client: AsyncClient):
    _, alice = await register_and_login(client)
    _, bob = await register_and_login(client, email="bob@example.com")
    alice_notes = {await _create_text_item(client, alice, f"alice {i}") for i in range(5)}
    alice_link = await _create_text_item(client, alice, "https://example.com")
    for i in range(5):
        await _create_text_item(client, bob, f"bob {i}")

    response = await client.get(
        "/items", params={"sort": "random", "type": "text"}, headers={"Authorization": f"Bearer {alice}"}
    )

    assert response.status_code == 200
    body = response.json()
    # All of Alice's notes, none of Bob's items, not her link; one sample, no pages.
    assert {item["id"] for item in body["items"]} == alice_notes
    assert alice_link not in {item["id"] for item in body["items"]}
    assert body["next_cursor"] is None


async def test_list_items_random_is_a_sample_of_limit_items(client: AsyncClient):
    _, token = await register_and_login(client)
    ids = {await _create_text_item(client, token, f"item {i}") for i in range(5)}

    response = await client.get(
        "/items", params={"sort": "random", "limit": 2}, headers={"Authorization": f"Bearer {token}"}
    )

    items = response.json()["items"]
    assert len(items) == 2
    assert {item["id"] for item in items} <= ids
    assert response.json()["next_cursor"] is None


async def test_list_items_rejects_unknown_sort(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.get("/items", params={"sort": "alphabetical"}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 422
