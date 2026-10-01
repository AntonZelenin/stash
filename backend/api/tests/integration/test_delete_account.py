import uuid
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AccessToken, RefreshToken
from app.items.models import (
    Collection,
    Description,
    FileMetadata,
    ImageMetadata,
    Item,
    ItemStatus,
    ItemType,
    PendingUpload,
    SearchChunk,
    Tag,
    TextContent,
    item_collections,
    item_tags,
)
from app.storage.deletions import StorageDeletionDrainer, storage_deletions
from app.users.models import User
from conftest import FakeObjectStorage
from helpers import register_and_login, start_upload, upload_file, upload_image

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5a1e8b10000000049454e44ae426082"
)
_PDF_BYTES = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"

_PASSWORD = "correct-horse"

# Every table holding a user's rows, by the column naming the owner; tables
# hanging off items are checked through the items' ids.
_USER_TABLES = {
    User.__table__: User.__table__.c.id,
    AccessToken.__table__: AccessToken.__table__.c.user_id,
    RefreshToken.__table__: RefreshToken.__table__.c.user_id,
    Item.__table__: Item.__table__.c.user_id,
    Tag.__table__: Tag.__table__.c.user_id,
    Collection.__table__: Collection.__table__.c.user_id,
    PendingUpload.__table__: PendingUpload.__table__.c.user_id,
}
_ITEM_TABLES = {
    TextContent.__table__: TextContent.__table__.c.item_id,
    ImageMetadata.__table__: ImageMetadata.__table__.c.item_id,
    FileMetadata.__table__: FileMetadata.__table__.c.item_id,
    Description.__table__: Description.__table__.c.item_id,
    SearchChunk.__table__: SearchChunk.__table__.c.item_id,
    item_tags: item_tags.c.item_id,
    item_collections: item_collections.c.item_id,
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _delete_account(client: AsyncClient, token: str, password: str = _PASSWORD):
    return await client.post("/users/me/delete", json={"password": password}, headers=_auth(token))


async def _populate(client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, email: str):
    """A user with some of everything: a note with tags in a collection, a
    captioned image (with a thumbnail, as the worker would store it), a
    file, search chunks, an upload started but never finalized, and a
    refresh token. Returns (user id, access token, refresh token)."""
    user_id, token = await register_and_login(client, email=email, password=_PASSWORD)
    login = (await client.post("/login", json={"email": email, "password": _PASSWORD})).json()
    note = await client.post(
        "/items/text",
        json={"text": "asyncio notes", "tags": ["Python"], "collections": ["Reading"]},
        headers=_auth(token),
    )
    assert note.status_code == 202, note.text
    image = await upload_image(client, storage, token, _PNG_BYTES, text="caption", tags=["Photo"])
    assert image.status_code == 202, image.text
    assert (await upload_file(client, storage, token, "Report.pdf", _PDF_BYTES)).status_code == 202
    assert (await start_upload(client, token, type="file", filename="Later.pdf", size_bytes=10)).status_code == 201

    image_id = UUID(image.json()["id"])
    thumbnail_key = f"users/{user_id}/thumbnails/{image_id}.webp"
    storage.uploads[thumbnail_key] = (b"webp", "image/webp")
    (await session.get(ImageMetadata, image_id)).thumbnail_key = thumbnail_key
    session.add(SearchChunk(item_id=UUID(note.json()["id"]), position=0, text="asyncio notes", embedding="[0]"))
    await session.commit()
    return user_id, token, login["refresh_token"]


async def _rows_of(session: AsyncSession, user_id: str) -> dict[str, int]:
    """How many rows of each table are the user's."""
    owner = UUID(user_id)
    item_ids = select(Item.id).where(Item.user_id == owner)
    counts = {}
    for table, column in _USER_TABLES.items():
        counts[table.name] = await session.scalar(select(func.count()).select_from(table).where(column == owner))
    for table, column in _ITEM_TABLES.items():
        counts[table.name] = await session.scalar(
            select(func.count()).select_from(table).where(column.in_(item_ids))
        )
    return counts


def _keys_of(storage: FakeObjectStorage, user_id: str) -> list[str]:
    return [key for key in storage.uploads if user_id in key]


async def _pending_deletions(session: AsyncSession) -> list:
    return (await session.execute(select(storage_deletions).order_by(storage_deletions.c.target))).all()


async def test_delete_account(client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage):
    user_id, token, _ = await _populate(client, session, storage, "alice@example.com")
    session.expire_all()
    assert all((await _rows_of(session, user_id)).values()), "every table should start with some of the user's rows"
    assert _keys_of(storage, user_id)

    response = await _delete_account(client, token)

    assert response.status_code == 204, response.text
    session.expire_all()
    assert not any((await _rows_of(session, user_id)).values())
    # Every object, staged uploads included, is gone; nothing is left to do.
    assert _keys_of(storage, user_id) == []
    assert await _pending_deletions(session) == []


async def test_sessions_end_and_the_account_is_gone(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token, refresh_token = await _populate(client, session, storage, "alice@example.com")

    assert (await _delete_account(client, token)).status_code == 204

    assert (await client.get("/users/me", headers=_auth(token))).status_code == 401
    assert (await client.get("/items", headers=_auth(token))).status_code == 401
    assert (await client.post("/refresh", json={"refresh_token": refresh_token})).status_code == 401
    login = await client.post("/login", json={"email": "alice@example.com", "password": _PASSWORD})
    assert login.status_code == 401
    # The email can be registered again, as a new account.
    again = await client.post("/users", json={"email": "alice@example.com", "password": _PASSWORD})
    assert again.status_code == 201


async def test_other_users_are_unaffected(client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage):
    _, alice_token, _ = await _populate(client, session, storage, "alice@example.com")
    bob_id, bob_token, bob_refresh = await _populate(client, session, storage, "bob@example.com")
    session.expire_all()
    bob_rows = await _rows_of(session, bob_id)
    bob_objects = {key: storage.uploads[key] for key in _keys_of(storage, bob_id)}
    bob_items = (await client.get("/items", headers=_auth(bob_token))).json()["items"]

    assert (await _delete_account(client, alice_token)).status_code == 204

    session.expire_all()
    assert await _rows_of(session, bob_id) == bob_rows
    assert {key: storage.uploads[key] for key in _keys_of(storage, bob_id)} == bob_objects
    assert (await client.get("/items", headers=_auth(bob_token))).json()["items"] == bob_items
    assert (await client.post("/refresh", json={"refresh_token": bob_refresh})).status_code == 200


async def test_storage_failure_leaves_the_deletion_pending_until_a_later_drain(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """The database deletion doesn't wait for storage: it's committed, and
    the objects' deletion stays recorded until a drain manages it."""
    user_id, token, _ = await _populate(client, session, storage, "alice@example.com")
    storage.fail_deletes = True

    assert (await _delete_account(client, token)).status_code == 204

    session.expire_all()
    assert not any((await _rows_of(session, user_id)).values())
    assert _keys_of(storage, user_id)
    pending = await _pending_deletions(session)
    assert [(row.target, row.is_prefix) for row in pending] == [
        (f"uploads/{user_id}/", True),
        (f"users/{user_id}/", True),
    ]
    # The drain stops at the first failure: storage is likely down.
    assert sorted(row.attempts for row in pending) == [0, 1]

    storage.fail_deletes = False
    assert await StorageDeletionDrainer(session.bind, storage).drain() == 2

    assert _keys_of(storage, user_id) == []
    session.expire_all()
    assert await _pending_deletions(session) == []


async def test_objects_stored_under_the_legacy_layout_are_deleted(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client, password=_PASSWORD)
    image_id = uuid.uuid4()
    session.add(
        Item(
            id=image_id,
            user_id=UUID(user_id),
            type=ItemType.image,
            status=ItemStatus.completed,
            image=ImageMetadata(
                storage_key=f"images/{image_id}.png",
                content_type="image/png",
                size_bytes=len(_PNG_BYTES),
                thumbnail_key=f"thumbnails/{image_id}.webp",
            ),
        )
    )
    await session.commit()
    storage.uploads[f"images/{image_id}.png"] = (_PNG_BYTES, "image/png")
    storage.uploads[f"thumbnails/{image_id}.webp"] = (b"webp", "image/webp")
    storage.uploads["images/someone-elses.png"] = (_PNG_BYTES, "image/png")

    assert (await _delete_account(client, token)).status_code == 204

    assert list(storage.uploads) == ["images/someone-elses.png"]


async def test_wrong_password_deletes_nothing(client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage):
    user_id, token, _ = await _populate(client, session, storage, "alice@example.com")
    session.expire_all()
    rows = await _rows_of(session, user_id)
    objects = dict(storage.uploads)

    response = await _delete_account(client, token, password="not-my-password")

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "password"]
    session.expire_all()
    assert await _rows_of(session, user_id) == rows
    assert storage.uploads == objects
    assert await _pending_deletions(session) == []
    assert (await client.get("/users/me", headers=_auth(token))).status_code == 200


async def test_wrong_passwords_are_rate_limited(client: AsyncClient, rate_limits):
    rate_limits(password_change_failures_per_user="2/15m")
    _, token = await register_and_login(client, password=_PASSWORD)

    for _ in range(2):
        assert (await _delete_account(client, token, password="guess")).status_code == 422
    assert (await _delete_account(client, token, password="guess")).status_code == 429
    assert (await _delete_account(client, token)).status_code == 429


async def test_delete_account_requires_auth(client: AsyncClient):
    assert (await client.post("/users/me/delete", json={"password": _PASSWORD})).status_code == 401
