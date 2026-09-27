"""Finalized upload content is immutable (security finding M1): the bytes
finalize validated are the bytes the item has for good, whatever happens
to the staging object the client uploaded to, and whatever the upload URL
is used for afterwards (see `stash_shared.storage_keys`)."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared import storage_keys

from app.items import files
from app.items.models import FileMetadata, ImageMetadata, Item, PendingUpload
from conftest import FakeJobQueue, FakeObjectStorage
from helpers import canonical_image_key, finalize_upload, register_and_login, start_upload

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360000000020001e221bc330000000049454e"
    "44ae426082"
)
# Same size and signed type, different (and invalid) content: what an
# attacker would swap in after validation.
_SWAPPED = b"<script>alert(1)</script>".ljust(len(_PNG_BYTES), b" ")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _start_png(client: AsyncClient, token: str) -> dict:
    response = await start_upload(client, token, type="image", content_type="image/png", size_bytes=len(_PNG_BYTES))
    assert response.status_code == 201, response.text
    return response.json()


def _put(storage: FakeObjectStorage, started: dict, data: bytes = _PNG_BYTES) -> None:
    storage.put(started["upload"]["url"], started["upload"]["headers"], data)


def _staging_key(user_id: str, started: dict) -> str:
    return storage_keys.staging_key(UUID(user_id), UUID(started["upload_id"]))


async def _item_count(session: AsyncSession) -> int:
    return len((await session.execute(Item.__table__.select())).all())


async def _finalized_png(client: AsyncClient, storage: FakeObjectStorage) -> tuple[str, str, dict]:
    user_id, token = await register_and_login(client)
    started = await _start_png(client, token)
    _put(storage, started)
    assert (await finalize_upload(client, token, started["upload_id"])).status_code == 202
    return user_id, token, started


async def test_replacing_the_staging_object_after_finalize_does_not_change_the_item(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    user_id, token, started = await _finalized_png(client, storage)
    canonical = canonical_image_key(storage, user_id, started["upload_id"], _PNG_BYTES, ".png")

    # Anything at all written to the staging key afterwards.
    storage.overwrite(_staging_key(user_id, started), _SWAPPED)

    image = await session.get(ImageMetadata, UUID(started["upload_id"]))
    assert (image.storage_key, image.content_etag) == (canonical, storage.etag_of(_PNG_BYTES))
    assert image.content_sha256 == hashlib.sha256(_PNG_BYTES).hexdigest()
    assert storage.data(canonical) == _PNG_BYTES
    [listed] = (await client.get("/items", headers=_auth(token))).json()["items"]
    assert listed["download_url"].startswith(f"https://fake-storage.test/{canonical}?")


async def test_upload_url_cannot_replace_what_it_uploaded(client: AsyncClient, storage: FakeObjectStorage):
    """The URL is create-only (a signed `If-None-Match: *`): once the
    upload arrived, it can't be swapped for other bytes, before finalize
    or during it."""
    _, token = await register_and_login(client)
    started = await _start_png(client, token)
    _put(storage, started)

    try:
        _put(storage, started, _SWAPPED)
    except FileExistsError:
        pass
    else:
        raise AssertionError("a second PUT with the same URL was accepted")


async def test_stale_upload_url_cannot_affect_finalized_content(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, queue: FakeJobQueue
):
    """The staging object is deleted on finalize, so the URL (valid until
    it expires) can stage something again. That's never looked at: the
    upload is no longer pending, and a replayed finalize returns the item
    as it is."""
    user_id, token, started = await _finalized_png(client, storage)
    canonical = canonical_image_key(storage, user_id, started["upload_id"], _PNG_BYTES, ".png")

    _put(storage, started, _SWAPPED)
    replayed = await finalize_upload(client, token, started["upload_id"])

    assert replayed.status_code == 202 and replayed.json()["id"] == started["upload_id"]
    assert len(storage.copies) == 1
    assert len(queue.published) == 1
    assert storage.data(canonical) == _PNG_BYTES
    session.expire_all()
    assert (await session.get(ImageMetadata, UUID(started["upload_id"]))).storage_key == canonical


async def test_staging_replaced_between_validation_and_copy_creates_nothing(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, queue: FakeJobQueue
):
    """The copy is pinned to the ETag of what was validated: content
    swapped in after the check (by any writer) is refused, the upload stays
    pending, and a later finalize validates what's actually there."""
    user_id, token = await register_and_login(client)
    started = await _start_png(client, token)
    _put(storage, started)
    staging = _staging_key(user_id, started)
    storage.before_copy = lambda: storage.overwrite(staging, _SWAPPED)

    response = await finalize_upload(client, token, started["upload_id"])

    assert response.status_code == 409
    assert await _item_count(session) == 0
    assert queue.published == []
    assert storage.copies == []
    assert list(storage.uploads) == [staging]
    assert await session.get(PendingUpload, UUID(started["upload_id"])) is not None

    # Finalizing again checks the swapped-in content, which is invalid.
    storage.before_copy = None
    response = await finalize_upload(client, token, started["upload_id"])

    assert response.status_code == 422
    assert await _item_count(session) == 0
    assert storage.uploads == {}


async def test_failed_validation_leaves_no_canonical_object(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)
    started = await _start_png(client, token)
    _put(storage, started, _SWAPPED)

    response = await finalize_upload(client, token, started["upload_id"])

    assert response.status_code == 422
    assert storage.copies == []
    assert storage.uploads == {}
    assert await _item_count(session) == 0


async def test_staging_swapped_under_the_same_etag_is_caught_on_the_copy(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, queue: FakeJobQueue
):
    """The ETag pin isn't trusted as a content hash: content swapped in
    with the validated object's ETag (an MD5 collision, or a storage whose
    ETags don't hash content) passes the copy's condition, but the copy
    itself is checked against what was validated. Nothing is created, and
    the rejected copy is removed."""
    user_id, token = await register_and_login(client)
    started = await _start_png(client, token)
    _put(storage, started)
    staging = _staging_key(user_id, started)
    storage.before_copy = lambda: storage.overwrite(staging, _SWAPPED, keep_etag=True)

    response = await finalize_upload(client, token, started["upload_id"])

    assert response.status_code == 409
    assert len(storage.copies) == 1
    assert list(storage.uploads) == [staging]
    assert await _item_count(session) == 0
    assert queue.published == []
    assert await session.get(PendingUpload, UUID(started["upload_id"])) is not None


async def test_recorded_sha256_is_of_the_stored_copy_not_the_validated_etag(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """Swapped under the same ETag for bytes validation can't tell apart
    (same size and first `SNIFF_BYTES`, all it looks at): whatever the
    copy holds, the item's SHA-256 is that content's, computed by the
    storage from the copy."""
    user_id, token = await register_and_login(client)
    data = _PNG_BYTES + b"\x00" * files.SNIFF_BYTES
    response = await start_upload(client, token, type="image", content_type="image/png", size_bytes=len(data))
    started = response.json()
    _put(storage, started, data)
    swapped = data[:-1] + b"\xff"
    storage.before_copy = lambda: storage.overwrite(_staging_key(user_id, started), swapped, keep_etag=True)

    assert (await finalize_upload(client, token, started["upload_id"])).status_code == 202

    image = await session.get(ImageMetadata, UUID(started["upload_id"]))
    assert storage.data(image.storage_key) == swapped
    assert image.content_sha256 == hashlib.sha256(swapped).hexdigest()


async def test_multipart_style_etag_is_just_a_token(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """An S3 Multipart Upload's ETag ("<hex>-<parts>") is no MD5 of the
    content. Nothing depends on its format: it only pins the object, and
    the content's identity is the copy's SHA-256."""
    user_id, token = await register_and_login(client)
    started = await _start_png(client, token)
    _put(storage, started)
    storage.etags[_staging_key(user_id, started)] = '"0123456789abcdef0123456789abcdef-3"'

    assert (await finalize_upload(client, token, started["upload_id"])).status_code == 202

    image = await session.get(ImageMetadata, UUID(started["upload_id"]))
    assert image.content_sha256 == hashlib.sha256(_PNG_BYTES).hexdigest()
    assert storage.data(image.storage_key) == _PNG_BYTES


async def test_canonical_keys_are_random_not_derived_from_content(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """Identical content (same ETag, same SHA-256) uploaded twice gets two
    unrelated keys: a key names one object, it doesn't address content."""
    user_id, token = await register_and_login(client)
    images = []
    for _ in range(2):
        started = await _start_png(client, token)
        _put(storage, started)
        assert (await finalize_upload(client, token, started["upload_id"])).status_code == 202
        images.append(await session.get(ImageMetadata, UUID(started["upload_id"])))

    first, second = (image.storage_key.rsplit("/", 1)[1].removesuffix(".png") for image in images)
    assert first != second
    sha256, md5 = hashlib.sha256(_PNG_BYTES).hexdigest(), hashlib.md5(_PNG_BYTES).hexdigest()
    for object_id in (first, second):
        assert object_id not in (sha256[:32], md5) and object_id not in sha256


async def test_finalize_retried_after_copying_but_before_committing_makes_a_fresh_copy(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """A finalize that died between the copy and the commit left a
    canonical object no item points at. The retry never adopts it (nothing
    at a key is trusted by name): it copies again to a new key, and the
    leftover is only an orphan."""
    user_id, token = await register_and_login(client)
    started = await _start_png(client, token)
    _put(storage, started)
    staging = _staging_key(user_id, started)
    leftover = storage_keys.image_key(UUID(user_id), UUID(started["upload_id"]), storage_keys.new_object_id(), ".png")
    await storage.copy_immutable(
        source_key=staging, source_etag=storage.etag(staging), dest_key=leftover, content_type="image/png"
    )

    response = await finalize_upload(client, token, started["upload_id"])

    assert response.status_code == 202
    image = await session.get(ImageMetadata, UUID(started["upload_id"]))
    assert image.storage_key != leftover
    assert storage.data(image.storage_key) == _PNG_BYTES
    assert sorted(storage.uploads) == sorted([leftover, image.storage_key])


async def test_http_form_data_is_not_an_upload_path(client: AsyncClient, storage: FakeObjectStorage):
    """Uploads are started with JSON metadata; the bytes only ever go to
    storage with the pre-signed PUT. A multipart/form-data request carrying
    a file is refused, and stores nothing. (A text file: FastAPI's default
    422 response echoes the body, which fails on non-UTF-8 bytes.)"""
    _, token = await register_and_login(client)
    content = b"not an upload"

    response = await client.post(
        "/uploads",
        headers=_auth(token),
        data={"type": "file", "filename": "a.txt", "content_type": "text/plain", "size_bytes": str(len(content))},
        files={"file": ("a.txt", content, "text/plain")},
    )

    assert response.status_code == 422
    assert storage.uploads == {} and storage.signed_uploads == {}


async def test_unfinalized_upload_is_never_visible_or_served(
    client: AsyncClient, storage: FakeObjectStorage
):
    """Staged, but not (yet) finalized: no route knows it as an item, so no
    URL is ever issued for its staging object."""
    _, token = await register_and_login(client)
    started = await _start_png(client, token)
    _put(storage, started)
    upload_id = started["upload_id"]

    assert (await client.get("/items", headers=_auth(token))).json()["items"] == []
    assert (await client.get(f"/items/{upload_id}", headers=_auth(token))).status_code == 404
    assert (await client.get(f"/items/{upload_id}/playback-url", headers=_auth(token))).status_code == 404
    assert storage.download_content_types == {}


async def test_finalized_items_are_only_ever_served_from_canonical_keys(
    client: AsyncClient, storage: FakeObjectStorage
):
    _, token, _started = await _finalized_png(client, storage)
    await client.post(
        "/uploads", json={"type": "image", "content_type": "image/png", "size_bytes": 5}, headers=_auth(token)
    )

    [listed] = (await client.get("/items", headers=_auth(token))).json()["items"]
    item = (await client.get(f"/items/{listed['id']}", headers=_auth(token))).json()

    assert f"/{storage_keys.STAGING_PREFIX}" not in listed["download_url"]
    assert f"/{storage_keys.STAGING_PREFIX}" not in item["download_url"]
    assert not any(storage_keys.is_staging_key(key) for key in storage.download_content_types)


async def test_abandoned_upload_can_no_longer_be_finalized(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)
    started = await _start_png(client, token)
    _put(storage, started)
    pending = await session.get(PendingUpload, UUID(started["upload_id"]))
    pending.expires_at = datetime.now(UTC) - timedelta(days=2)
    await session.commit()

    response = await finalize_upload(client, token, started["upload_id"])

    assert response.status_code == 404
    assert storage.copies == []
    # Its staging object is removed with it.
    assert storage.uploads == {}
    session.expire_all()
    assert await session.get(PendingUpload, UUID(started["upload_id"])) is None


async def test_abandoned_uploads_are_purged_when_the_user_starts_another(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """Only rows: staging objects are left to the bucket's lifecycle rule.
    Recent pending uploads, and other users', are kept."""
    _, token = await register_and_login(client)
    _, other_token = await register_and_login(client, email="bob@example.com")
    abandoned = await _start_png(client, token)
    recent = await _start_png(client, token)
    others = await _start_png(client, other_token)
    for started in (abandoned, others):
        pending = await session.get(PendingUpload, UUID(started["upload_id"]))
        pending.expires_at = datetime.now(UTC) - timedelta(days=2)
    await session.commit()

    await _start_png(client, token)

    session.expire_all()
    assert await session.get(PendingUpload, UUID(abandoned["upload_id"])) is None
    assert await session.get(PendingUpload, UUID(recent["upload_id"])) is not None
    assert await session.get(PendingUpload, UUID(others["upload_id"])) is not None


async def test_legacy_items_keep_working(client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage):
    """Items finalized before canonical copies existed keep their old key
    and have no ETag; they're served exactly as before."""
    user_id, token = await register_and_login(client)
    legacy_key = f"users/{user_id}/files/legacy.pdf"
    item = Item(
        user_id=UUID(user_id),
        type="file",
        status="completed",
        file=FileMetadata(
            storage_key=legacy_key, filename="old.pdf", content_type="application/pdf", size_bytes=10
        ),
    )
    session.add(item)
    await session.commit()

    [listed] = (await client.get("/items", headers=_auth(token))).json()["items"]

    assert listed["download_url"].startswith(f"https://fake-storage.test/{legacy_key}?")
    assert (await session.get(FileMetadata, item.id)).content_etag is None
