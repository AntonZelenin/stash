import uuid
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Description, ImageMetadata, Item, TextContent
from conftest import FakeObjectStorage
from helpers import register_and_login

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5a1e8b10000000049454e44ae426082"
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_delete_image_item_removes_rows_and_stored_file(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)
    created = await client.post(
        "/items/image",
        files={"file": ("photo.png", _PNG_BYTES, "image/png")},
        data={"text": "caption"},
        headers=_auth(token),
    )
    item_id = UUID(created.json()["id"])
    [storage_key] = storage.uploads

    response = await client.delete(f"/items/{item_id}", headers=_auth(token))

    assert response.status_code == 204
    session.expire_all()
    for model in (Item, ImageMetadata, TextContent, Description):
        assert await session.get(model, item_id) is None, model.__name__
    assert storage_key not in storage.uploads
    listed = (await client.get("/items", headers=_auth(token))).json()["items"]
    assert listed == []


async def test_delete_text_item(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    created = await client.post("/items/text", json={"text": "hello"}, headers=_auth(token))
    item_id = UUID(created.json()["id"])

    response = await client.delete(f"/items/{item_id}", headers=_auth(token))

    assert response.status_code == 204
    session.expire_all()
    assert await session.get(Item, item_id) is None
    assert await session.get(Description, item_id) is None


async def test_cannot_delete_another_users_item(client: AsyncClient, session: AsyncSession):
    _, owner_token = await register_and_login(client, email="owner@example.com")
    created = await client.post("/items/text", json={"text": "mine"}, headers=_auth(owner_token))
    item_id = UUID(created.json()["id"])
    _, other_token = await register_and_login(client, email="other@example.com")

    response = await client.delete(f"/items/{item_id}", headers=_auth(other_token))

    # Same answer as for a missing item, so ids of other users' items
    # can't be probed.
    assert response.status_code == 404
    session.expire_all()
    assert await session.get(Item, item_id) is not None


async def test_delete_missing_item_is_404(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.delete(f"/items/{uuid.uuid4()}", headers=_auth(token))

    assert response.status_code == 404


async def test_delete_requires_token(client: AsyncClient):
    response = await client.delete(f"/items/{uuid.uuid4()}")

    assert response.status_code == 401


async def test_delete_image_item_also_removes_thumbnail(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client)
    created = await client.post(
        "/items/image", files={"file": ("photo.png", _PNG_BYTES, "image/png")}, headers=_auth(token)
    )
    item_id = UUID(created.json()["id"])
    # As the thumbnail worker would have left it.
    image = await session.get(ImageMetadata, item_id)
    image.thumbnail_key = f"users/{user_id}/thumbnails/{item_id}.webp"
    await session.commit()
    storage.uploads[image.thumbnail_key] = (b"webp", "image/webp")

    response = await client.delete(f"/items/{item_id}", headers=_auth(token))

    assert response.status_code == 204
    assert storage.uploads == {}
