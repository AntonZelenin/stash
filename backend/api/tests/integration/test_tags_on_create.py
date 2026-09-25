import pytest
from httpx import AsyncClient

from conftest import FakeObjectStorage
from helpers import register_and_login, upload_file, upload_image

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5a1e8b10000000049454e44ae426082"
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _tag_names(client: AsyncClient, token: str) -> list[list[str]]:
    items = (await client.get("/items", headers=_auth(token))).json()["items"]
    return [[t["name"] for t in item["tags"]] for item in items]


async def test_text_item_created_with_tags(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.post(
        "/items/text", json={"text": "asyncio notes", "tags": ["Python", "Concurrency"]}, headers=_auth(token)
    )

    assert response.status_code == 202
    assert await _tag_names(client, token) == [["Concurrency", "Python"]]


async def test_existing_tags_are_reused_and_duplicates_dropped(client: AsyncClient):
    _, token = await register_and_login(client)
    await client.post("/items/text", json={"text": "first", "tags": ["Python"]}, headers=_auth(token))

    await client.post(
        "/items/text", json={"text": "second", "tags": ["python", " PYTHON ", "AWS"]}, headers=_auth(token)
    )

    # Order-independent: both items can share a created_at second.
    assert sorted(await _tag_names(client, token)) == [["AWS", "Python"], ["Python"]]
    tags = (await client.get("/tags", headers=_auth(token))).json()["tags"]
    assert [t["name"] for t in tags] == ["AWS", "Python"]


async def test_image_created_with_tags(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)

    response = await upload_image(client, storage, token, _PNG_BYTES, text="our cat", tags=['Pets', 'Home'])

    assert response.status_code == 202
    assert await _tag_names(client, token) == [["Home", "Pets"]]


async def test_file_created_with_tags(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)

    response = await upload_file(client, storage, token, "setup.exe", b"MZ\x90\x00", tags=['Drivers'])

    assert response.status_code == 202
    assert await _tag_names(client, token) == [["Drivers"]]


async def test_items_without_tags_still_work(client: AsyncClient):
    _, token = await register_and_login(client)

    await client.post("/items/text", json={"text": "plain"}, headers=_auth(token))

    assert await _tag_names(client, token) == [[]]


@pytest.mark.parametrize("tags", [["   "], ["x" * 51], [f"t{i}" for i in range(21)]])
async def test_invalid_tags_reject_the_item_before_anything_is_stored(
    client: AsyncClient, storage: FakeObjectStorage, tags
):
    _, token = await register_and_login(client)

    text_response = await client.post("/items/text", json={"text": "note", "tags": tags}, headers=_auth(token))
    image_response = await upload_image(client, storage, token, _PNG_BYTES, tags=tags)

    assert text_response.status_code == 422
    assert image_response.status_code == 422
    assert (await client.get("/items", headers=_auth(token))).json()["items"] == []
    assert storage.uploads == {}
    assert (await client.get("/tags", headers=_auth(token))).json()["tags"] == []
