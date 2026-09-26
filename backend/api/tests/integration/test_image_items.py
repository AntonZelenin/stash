from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared import storage_keys
from stash_shared.queue.base import ItemType as QueueItemType

from app.items.models import Description, ImageMetadata, Item, ItemType, PendingUpload, TextContent
from app.items.services import _MAX_IMAGE_SIZE_BYTES
from conftest import FakeJobQueue, FakeObjectStorage
from helpers import finalize_upload, register_and_login, start_upload, upload_image

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360000000020001e221bc330000000049454e"
    "44ae426082"
)


async def test_create_image_item_persists_and_associates_with_user(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client)

    response = await upload_image(client, storage, token, _PNG_BYTES, filename="photo.png")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"

    item = await session.get(Item, UUID(body["id"]))
    assert item is not None
    assert item.type == ItemType.image
    assert str(item.user_id) == user_id

    image = await session.get(ImageMetadata, item.id)
    assert image is not None
    assert image.content_type == "image/png"
    assert image.size_bytes == len(_PNG_BYTES)
    assert image.filename == "photo.png"
    # Under the owner's prefix, named by item id only (never the upload's
    # own filename).
    assert image.storage_key == f"users/{user_id}/images/{item.id}.png"

    uploaded_data, uploaded_content_type = storage.uploads[image.storage_key]
    assert uploaded_data == _PNG_BYTES
    assert uploaded_content_type == "image/png"


async def test_start_upload_signs_a_url_for_a_generated_user_scoped_key(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client)

    response = await start_upload(
        client, token, type="image", size_bytes=len(_PNG_BYTES), content_type="image/jpeg", filename="../../x.png"
    )

    assert response.status_code == 201
    body = response.json()
    upload_id = body["upload_id"]
    key = f"users/{user_id}/images/{upload_id}.jpg"
    # Signed for that key, type and exact size only.
    assert storage.signed_uploads == {key: ("image/jpeg", len(_PNG_BYTES))}
    assert body["upload"] == {
        "url": f"https://fake-storage.test/upload/{key}?expires_in=900",
        "method": "PUT",
        "headers": {"Content-Type": "image/jpeg"},
    }
    assert body["expires_at"]
    # Nothing exists yet but the pending upload.
    assert await session.get(Item, UUID(upload_id)) is None
    pending = await session.get(PendingUpload, UUID(upload_id))
    assert (pending.storage_key, str(pending.user_id)) == (key, user_id)


async def test_create_image_item_rejects_empty_file(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)

    response = await upload_image(client, storage, token, b"")

    assert response.status_code == 422
    assert response.json()["detail"] == "File is empty"
    assert storage.signed_uploads == {}


async def test_create_image_item_rejects_unsupported_declared_type(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)

    response = await upload_image(client, storage, token, _PNG_BYTES, content_type="image/heic")

    assert response.status_code == 422
    assert response.json()["detail"] == "Unsupported image type"
    assert storage.signed_uploads == {}


async def test_create_image_item_rejects_non_image_content(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """Declared as a PNG, but what arrived isn't one: no item, and the
    upload is discarded, object included."""
    _, token = await register_and_login(client)

    response = await upload_image(client, storage, token, b"just some text", content_type="image/png")

    assert response.status_code == 422
    assert response.json()["detail"] == "Unsupported image type"
    assert storage.uploads == {}
    assert (await session.execute(Item.__table__.select())).first() is None
    assert (await session.execute(PendingUpload.__table__.select())).first() is None


async def test_create_image_item_rejects_content_of_another_image_type(
    client: AsyncClient, storage: FakeObjectStorage
):
    """The key's extension and the type the object is served with come
    from the declared type, so the content must be exactly that."""
    _, token = await register_and_login(client)

    response = await upload_image(client, storage, token, _PNG_BYTES, content_type="image/jpeg")

    assert response.status_code == 422
    assert storage.uploads == {}


async def test_create_image_item_rejects_oversized_file(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)

    response = await start_upload(client, token, type="image", size_bytes=_MAX_IMAGE_SIZE_BYTES + 1, content_type="image/png")

    assert response.status_code == 422
    assert response.json()["detail"] == "File is too large"
    assert storage.signed_uploads == {}


async def test_create_image_item_rejects_missing_token(client: AsyncClient):
    response = await client.post("/uploads", json={"type": "image", "size_bytes": 10, "content_type": "image/png"})

    assert response.status_code == 401


async def test_create_image_item_publishes_processing_job(
    client: AsyncClient, queue: FakeJobQueue, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client)
    started = await start_upload(client, token, type="image", size_bytes=len(_PNG_BYTES), content_type="image/png")
    storage.put(started.json()["upload"]["url"], started.json()["upload"]["headers"], _PNG_BYTES)

    # Nothing is processed until the upload is finalized.
    assert queue.published == []

    response = await finalize_upload(client, token, started.json()["upload_id"])

    assert response.status_code == 202
    assert response.json()["id"] == started.json()["upload_id"]
    assert len(queue.published) == 1
    job = queue.published[0]
    assert str(job.item_id) == response.json()["id"]
    assert str(job.user_id) == user_id
    assert job.item_type == QueueItemType.image
    # The worker fetches the bytes from storage using this, so it must point
    # at exactly what was uploaded.
    assert job.image is not None
    assert job.image.storage_key in storage.uploads
    assert job.image.content_type == "image/png"


async def test_image_whose_job_cannot_be_published_yet_stays_pending_until_a_later_request(
    client: AsyncClient, session: AsyncSession, queue: FakeJobQueue, storage: FakeObjectStorage
):
    """The job is in the outbox with the item: the next request that
    flushes it publishes it, and the item goes on to processing."""
    queue.fail_publish = True
    user_id, token = await register_and_login(client)

    response = await upload_image(client, storage, token, _PNG_BYTES)

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    item_id = UUID(response.json()["id"])
    item = await session.get(Item, item_id)
    assert item.status == "pending"
    assert queue.published == []

    queue.fail_publish = False
    await client.post("/items/text", json={"text": "later"}, headers={"Authorization": f"Bearer {token}"})

    [job] = queue.published
    assert job.item_id == item_id
    assert job.image.storage_key == f"users/{user_id}/images/{item_id}.png"
    assert str(job.user_id) == user_id


async def test_create_image_item_with_caption_stores_it_on_the_same_item(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)

    response = await upload_image(client, storage, token, _PNG_BYTES, text="  our cat Mochi  ")

    assert response.status_code == 202
    item_id = UUID(response.json()["id"])
    assert (await session.get(TextContent, item_id)).text == "our cat Mochi"
    # Searchable by the caption right away, before analysis finishes.
    assert (await session.get(Description, item_id)).text == "our cat Mochi"

    listed = (await client.get("/items", headers={"Authorization": f"Bearer {token}"})).json()["items"]
    assert listed[0]["type"] == "image"
    assert listed[0]["text"] == "our cat Mochi"
    assert listed[0]["download_url"] is not None


async def test_create_image_item_ignores_blank_caption(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)

    response = await upload_image(client, storage, token, _PNG_BYTES, text="   ")

    assert response.status_code == 202
    item_id = UUID(response.json()["id"])
    assert await session.get(TextContent, item_id) is None
    assert await session.get(Description, item_id) is None


async def test_listed_image_has_thumbnail_url_once_thumbnail_exists(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client)
    created = await upload_image(client, storage, token, _PNG_BYTES)
    item_id = UUID(created.json()["id"])

    listed = (await client.get("/items", headers={"Authorization": f"Bearer {token}"})).json()["items"]
    # Not generated yet: clients fall back to download_url.
    assert listed[0]["thumbnail_url"] is None
    assert listed[0]["download_url"] is not None

    image = await session.get(ImageMetadata, item_id)
    # As the thumbnail worker records it.
    image.thumbnail_key = storage_keys.thumbnail_key(UUID(user_id), item_id)
    await session.commit()

    listed = (await client.get("/items", headers={"Authorization": f"Bearer {token}"})).json()["items"]
    assert listed[0]["thumbnail_url"] == (
        f"https://fake-storage.test/users/{user_id}/thumbnails/{item_id}.webp?expires_in=3600"
    )


async def test_listed_image_downloads_under_its_original_filename(client: AsyncClient, storage: FakeObjectStorage):
    user_id, token = await register_and_login(client)
    created = await upload_image(client, storage, token, _PNG_BYTES, filename="Photos/Cat on the sofa.png")
    item_id = created.json()["id"]

    listed = (await client.get("/items", headers={"Authorization": f"Bearer {token}"})).json()["items"]
    # Path dropped; inline, so it still displays in the browser.
    assert listed[0]["download_url"] == (
        f"https://fake-storage.test/users/{user_id}/images/{item_id}.png"
        "?expires_in=3600&filename=Cat on the sofa.png&disposition=inline"
    )


async def test_image_uploaded_without_a_filename_downloads_unnamed(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client)
    created = await upload_image(client, storage, token, _PNG_BYTES, filename="  ")
    item_id = created.json()["id"]

    assert (await session.get(ImageMetadata, UUID(item_id))).filename is None
    listed = (await client.get("/items", headers={"Authorization": f"Bearer {token}"})).json()["items"]
    assert listed[0]["download_url"] == f"https://fake-storage.test/users/{user_id}/images/{item_id}.png?expires_in=3600"
