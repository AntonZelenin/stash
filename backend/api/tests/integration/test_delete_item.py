import uuid
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Description, ImageMetadata, Item, TextContent
from app.storage.deletions import StorageDeletionDrainer, schedule_key_deletion, storage_deletions
from conftest import FakeObjectStorage
from helpers import register_and_login, upload_file, upload_image

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
    created = await upload_image(client, storage, token, _PNG_BYTES, text="caption")
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
    created = await upload_image(client, storage, token, _PNG_BYTES)
    item_id = UUID(created.json()["id"])
    # As the thumbnail worker would have left it.
    image = await session.get(ImageMetadata, item_id)
    image.thumbnail_key = f"users/{user_id}/thumbnails/{item_id}.webp"
    await session.commit()
    storage.uploads[image.thumbnail_key] = (b"webp", "image/webp")

    response = await client.delete(f"/items/{item_id}", headers=_auth(token))

    assert response.status_code == 204
    assert storage.uploads == {}


async def test_objects_whose_deletion_fails_stay_pending_until_a_later_drain(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """Storage failing after the commit no longer orphans the item's
    objects: their deletion was recorded with the item's rows, and the
    scheduled drain carries it out once storage is back."""
    _, token = await register_and_login(client)
    created = await upload_image(client, storage, token, _PNG_BYTES)
    item_id = UUID(created.json()["id"])
    image = await session.get(ImageMetadata, item_id)
    image.thumbnail_key = f"users/thumbnails/{item_id}.webp"
    await session.commit()
    storage.uploads[image.thumbnail_key] = (b"webp", "image/webp")
    keys = set(storage.uploads)
    storage.fail_deletes = True

    response = await client.delete(f"/items/{item_id}", headers=_auth(token))

    assert response.status_code == 204
    session.expire_all()
    assert await session.get(Item, item_id) is None
    assert set(storage.uploads) == keys
    pending = await _pending_deletions(session)
    assert set(pending) == keys
    # The drain stopped at the first failure: storage is likely down.
    assert sorted(pending.values()) == [0, 1]

    storage.fail_deletes = False
    assert await StorageDeletionDrainer(session.bind, storage).drain() == 2
    assert storage.uploads == {}
    assert await _pending_deletions(session) == {}


async def test_a_request_drains_only_its_own_deletions(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """A backlog (e.g. left while storage was down) is the scheduled
    drain's, so a request never takes it on."""
    storage.uploads["users/backlog"] = (b"x", "text/plain")
    await schedule_key_deletion(session, "users/backlog")
    await session.commit()
    _, token = await register_and_login(client)
    created = await upload_image(client, storage, token, _PNG_BYTES)
    item_id = UUID(created.json()["id"])

    assert (await client.delete(f"/items/{item_id}", headers=_auth(token))).status_code == 204

    assert set(storage.uploads) == {"users/backlog"}
    assert await _pending_deletions(session) == {"users/backlog": 0}


async def _pending_deletions(session: AsyncSession) -> dict[str, int]:
    session.expire_all()
    rows = (await session.execute(select(storage_deletions))).all()
    return {row.target: row.attempts for row in rows}
