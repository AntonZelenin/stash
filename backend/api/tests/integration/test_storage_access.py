"""Stored objects: user-scoped keys, and access to them only through items
the requesting user owns (see `stash_shared.storage_keys`)."""

import uuid
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import FileMetadata, ImageMetadata, Item, ItemStatus, ItemType
from conftest import FakeObjectStorage
from helpers import register_and_login

_PDF_BYTES = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"
# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5a1e8b10000000049454e44ae426082"
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _upload_file(client: AsyncClient, token: str, filename: str = "report.pdf", data: bytes = _PDF_BYTES):
    response = await client.post(
        "/items/file", files={"file": (filename, data, "application/octet-stream")}, headers=_auth(token)
    )
    assert response.status_code == 202
    return UUID(response.json()["id"])


async def _upload_image(client: AsyncClient, token: str) -> UUID:
    response = await client.post(
        "/items/image", files={"file": ("photo.png", _PNG_BYTES, "image/png")}, headers=_auth(token)
    )
    assert response.status_code == 202
    return UUID(response.json()["id"])


async def _listed(client: AsyncClient, token: str) -> list[dict]:
    return (await client.get("/items", headers=_auth(token))).json()["items"]


# ---- key layout ----


async def test_same_filename_from_two_users_does_not_collide(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    alice_id, alice_token = await register_and_login(client, email="alice@example.com")
    bob_id, bob_token = await register_and_login(client, email="bob@example.com")
    alice_data, bob_data = _PDF_BYTES, _PDF_BYTES + b"% bob's copy\n"

    alice_item = await _upload_file(client, alice_token, "Report.pdf", alice_data)
    bob_item = await _upload_file(client, bob_token, "Report.pdf", bob_data)

    alice_file = await session.get(FileMetadata, alice_item)
    bob_file = await session.get(FileMetadata, bob_item)
    assert alice_file.storage_key == f"users/{alice_id}/files/{alice_item}.pdf"
    assert bob_file.storage_key == f"users/{bob_id}/files/{bob_item}.pdf"
    # Both objects kept, each with its own bytes.
    assert storage.uploads[alice_file.storage_key][0] == alice_data
    assert storage.uploads[bob_file.storage_key][0] == bob_data
    # The name they uploaded lives in the database only, and is what
    # downloads are served under.
    assert alice_file.filename == bob_file.filename == "Report.pdf"
    assert "Report" not in alice_file.storage_key
    [listed] = await _listed(client, bob_token)
    assert "filename=Report.pdf" in listed["download_url"]


async def test_uploaded_filename_never_shapes_the_key(client: AsyncClient, session: AsyncSession):
    user_id, token = await register_and_login(client)

    item_id = await _upload_file(client, token, "../../users/someone-else/files/x.pdf")

    stored = await session.get(FileMetadata, item_id)
    assert stored.storage_key == f"users/{user_id}/files/{item_id}.pdf"
    assert stored.filename == "x.pdf"


# ---- presigned access ----


async def test_owner_gets_presigned_urls_for_their_objects(client: AsyncClient, session: AsyncSession):
    user_id, token = await register_and_login(client)
    image_id = await _upload_image(client, token)
    file_id = await _upload_file(client, token)
    image = await session.get(ImageMetadata, image_id)
    image.thumbnail_key = f"users/{user_id}/thumbnails/{image_id}.webp"
    await session.commit()

    by_id = {UUID(item["id"]): item for item in await _listed(client, token)}

    image_item, file_item = by_id[image_id], by_id[file_id]
    assert image_item["download_url"].startswith(f"https://fake-storage.test/users/{user_id}/images/{image_id}.png?")
    assert image_item["thumbnail_url"].startswith(
        f"https://fake-storage.test/users/{user_id}/thumbnails/{image_id}.webp?"
    )
    assert file_item["download_url"].startswith(f"https://fake-storage.test/users/{user_id}/files/{file_id}.pdf?")
    # Short-lived: every URL carries its expiry.
    assert "expires_in=3600" in image_item["download_url"]


async def test_other_user_gets_no_access_to_an_item(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """Every route that could hand out a URL for, or touch the object of, an
    item answers someone else's item exactly like a missing one."""
    _, owner_token = await register_and_login(client, email="owner@example.com")
    _, other_token = await register_and_login(client, email="other@example.com")
    item_id = await _upload_file(client, owner_token)
    [key] = storage.uploads

    assert await _listed(client, other_token) == []
    edit = await client.patch(f"/items/{item_id}", json={"filename": "mine.pdf"}, headers=_auth(other_token))
    assert edit.status_code == 404
    assert "download_url" not in edit.text
    delete = await client.delete(f"/items/{item_id}", headers=_auth(other_token))
    assert delete.status_code == 404
    favorite = await client.put(f"/items/{item_id}/favorite", headers=_auth(other_token))
    assert favorite.status_code == 404

    # Nothing changed for the owner, and the object is still there.
    assert key in storage.uploads
    session.expire_all()
    assert (await session.get(FileMetadata, item_id)).filename == "report.pdf"
    [listed] = await _listed(client, owner_token)
    assert listed["download_url"].startswith(f"https://fake-storage.test/{key}?")


async def test_urls_are_only_issued_to_authenticated_users(client: AsyncClient):
    assert (await client.get("/items")).status_code == 401
    assert (await client.patch(f"/items/{uuid.uuid4()}", json={"text": "x"})).status_code == 401


async def test_client_cannot_point_an_item_at_another_users_object(client: AsyncClient, session: AsyncSession):
    """Keys are never accepted from clients: an edit carrying one (here,
    another user's object) is rejected outright, and the item keeps its own
    key — the only one ever signed for it."""
    _, victim_token = await register_and_login(client, email="victim@example.com")
    victim_item = await _upload_file(client, victim_token)
    victim_key = (await session.get(FileMetadata, victim_item)).storage_key
    user_id, token = await register_and_login(client, email="attacker@example.com")
    item_id = await _upload_file(client, token)

    response = await client.patch(
        f"/items/{item_id}", json={"text": "note", "storage_key": victim_key}, headers=_auth(token)
    )

    assert response.status_code == 422
    session.expire_all()
    assert (await session.get(FileMetadata, item_id)).storage_key == f"users/{user_id}/files/{item_id}.pdf"
    listed = await _listed(client, token)
    assert all(victim_key not in (item["download_url"] or "") for item in listed)


# ---- legacy keys ----


async def _insert_legacy_items(session: AsyncSession, user_id: str) -> tuple[UUID, UUID]:
    """An image (with thumbnail) and a file stored under the old, unscoped
    layout, as items created before user-scoped keys still are."""
    image_id, file_id = uuid.uuid4(), uuid.uuid4()
    owner = UUID(user_id)
    session.add_all(
        [
            Item(
                id=image_id,
                user_id=owner,
                type=ItemType.image,
                status=ItemStatus.completed,
                image=ImageMetadata(
                    storage_key=f"images/{image_id}.png",
                    content_type="image/png",
                    size_bytes=len(_PNG_BYTES),
                    thumbnail_key=f"thumbnails/{image_id}.webp",
                ),
            ),
            Item(
                id=file_id,
                user_id=owner,
                type=ItemType.file,
                status=ItemStatus.completed,
                file=FileMetadata(
                    storage_key=f"files/{file_id}.pdf",
                    filename="Old.pdf",
                    content_type="application/pdf",
                    size_bytes=len(_PDF_BYTES),
                ),
            ),
        ]
    )
    await session.commit()
    return image_id, file_id


async def test_legacy_keys_are_still_served_to_their_owner(client: AsyncClient, session: AsyncSession):
    user_id, token = await register_and_login(client)
    image_id, file_id = await _insert_legacy_items(session, user_id)

    by_id = {UUID(item["id"]): item for item in await _listed(client, token)}

    assert by_id[image_id]["download_url"].startswith(f"https://fake-storage.test/images/{image_id}.png?")
    assert by_id[image_id]["thumbnail_url"].startswith(f"https://fake-storage.test/thumbnails/{image_id}.webp?")
    assert by_id[file_id]["download_url"].startswith(f"https://fake-storage.test/files/{file_id}.pdf?")
    assert "filename=Old.pdf" in by_id[file_id]["download_url"]


async def test_legacy_keys_are_not_served_to_other_users(client: AsyncClient, session: AsyncSession):
    """With no user id in the key at all, ownership still comes from the
    item row alone."""
    owner_id, _ = await register_and_login(client, email="owner@example.com")
    _, other_token = await register_and_login(client, email="other@example.com")
    image_id, _ = await _insert_legacy_items(session, owner_id)

    assert await _listed(client, other_token) == []
    response = await client.patch(f"/items/{image_id}", json={"text": "x"}, headers=_auth(other_token))
    assert response.status_code == 404


async def test_deleting_a_legacy_item_removes_its_objects(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client)
    image_id, file_id = await _insert_legacy_items(session, user_id)
    for key in (f"images/{image_id}.png", f"thumbnails/{image_id}.webp", f"files/{file_id}.pdf"):
        storage.uploads[key] = (b"data", "application/octet-stream")

    assert (await client.delete(f"/items/{image_id}", headers=_auth(token))).status_code == 204
    assert (await client.delete(f"/items/{file_id}", headers=_auth(token))).status_code == 204

    assert storage.uploads == {}
