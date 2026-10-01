"""The reconciliation scan (`app.storage.reconciliation`): orphaned objects
under `users/` are deleted, including those left by races and crashes no
deletion was ever scheduled for, and nothing is deleted unless it's
certainly an orphan."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared import storage_keys

from app.items.models import ImageMetadata
from app.items.repos import ItemRepository
from app.storage.reconciliation import PREFIX, StorageReconciler, classify, storage_reconciliation
from conftest import FakeObjectStorage
from helpers import finalize_upload, register_and_login, start_upload, upload_file, upload_image

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5a1e8b10000000049454e44ae426082"
)
_USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
_ITEM = uuid.UUID("22222222-2222-4222-8222-222222222222")
_OBJECT_ID = "0123456789abcdef0123456789abcdef"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _reconciler(session: AsyncSession, storage: FakeObjectStorage, **kwargs) -> StorageReconciler:
    return StorageReconciler(session.bind, storage, **kwargs)


def _age(storage: FakeObjectStorage, *keys: str, by: timedelta = timedelta(days=2)) -> None:
    """Makes `keys` look last written `by` ago."""
    for key in keys:
        storage.modified[key] = datetime.now(UTC) - by


def _age_everything(storage: FakeObjectStorage) -> None:
    _age(storage, *storage.uploads)


def _store(storage: FakeObjectStorage, key: str, *, old: bool = True) -> str:
    storage.uploads[key] = (b"bytes", "application/octet-stream")
    if old:
        _age(storage, key)
    return key


async def _image_item(client: AsyncClient, storage: FakeObjectStorage, token: str) -> tuple[uuid.UUID, str]:
    """An image item's id and its original's key."""
    response = await upload_image(client, storage, token, _PNG_BYTES)
    assert response.status_code == 202, response.text
    item_id = uuid.UUID(response.json()["id"])
    [key] = [key for key in storage.uploads if key.startswith("users/") and str(item_id) in key]
    return item_id, key


async def _record_thumbnail(session: AsyncSession, storage: FakeObjectStorage, user_id: str, item_id: uuid.UUID):
    """What the thumbnail worker does: store, then record."""
    key = storage_keys.thumbnail_key(uuid.UUID(user_id), item_id)
    storage.uploads[key] = (b"webp", "image/webp")
    await session.execute(update(ImageMetadata).where(ImageMetadata.item_id == item_id).values(thumbnail_key=key))
    await session.commit()
    return key


# ---- What counts as an object the application writes ----


@pytest.mark.parametrize(
    "key",
    [
        f"users/{_USER}/images/{_ITEM}/{_OBJECT_ID}.png",
        f"users/{_USER}/files/{_ITEM}/{_OBJECT_ID}.pdf",
        f"users/{_USER}/files/{_ITEM}/{_OBJECT_ID}.fb2.zip",
        f"users/{_USER}/files/{_ITEM}/{_OBJECT_ID}",
        # Legacy: the key the upload URL itself wrote.
        f"users/{_USER}/images/{_ITEM}.jpg",
        f"users/{_USER}/thumbnails/{_ITEM}.webp",
    ],
)
def test_keys_the_application_writes_are_recognized(key: str):
    candidate = classify(key)

    assert candidate is not None and candidate.item_id == _ITEM


@pytest.mark.parametrize(
    "key",
    [
        f"users/{_USER}/",
        f"users/{_USER}/notes.txt",
        f"users/{_USER}/images/{_ITEM}/not-an-object-id.png",
        f"users/{_USER}/images/{_ITEM}/{_OBJECT_ID}.png/extra",
        f"users/{_USER}/images/not-an-item/{_OBJECT_ID}.png",
        f"users/{_USER}/thumbnails/{_ITEM}.png",
        f"users/{_USER}/backups/{_ITEM}/{_OBJECT_ID}.png",
        f"users/not-a-user/images/{_ITEM}/{_OBJECT_ID}.png",
        f"users/AAAAAAAA-1111-4111-8111-111111111111/images/{_ITEM}/{_OBJECT_ID}.png",
    ],
)
def test_anything_else_is_never_a_candidate(key: str):
    assert classify(key) is None


async def test_unrecognized_objects_are_left_alone_however_old(session: AsyncSession, storage: FakeObjectStorage):
    keys = [
        _store(storage, f"users/{_USER}/notes.txt"),
        _store(storage, f"users/{_USER}/images/{_ITEM}/not-an-object-id.png"),
        _store(storage, f"users/{_USER}/backups/{_ITEM}/{_OBJECT_ID}.png"),
    ]

    result = await _reconciler(session, storage).reconcile()

    assert result.deleted == 0 and result.unrecognized == 3
    assert all(key in storage.uploads for key in keys)


async def test_only_users_area_is_scanned(session: AsyncSession, storage: FakeObjectStorage):
    """Staging is the lifecycle rule's; anything outside both areas (the
    oldest, unscoped layout) isn't this scan's either."""
    keys = [
        _store(storage, f"uploads/{_USER}/{_ITEM}"),
        _store(storage, f"images/{_ITEM}.png"),
        _store(storage, f"thumbnails/{_ITEM}.webp"),
    ]

    result = await _reconciler(session, storage).reconcile()

    assert result.scanned == 0
    assert all(key in storage.uploads for key in keys)


# ---- Referenced objects are kept ----


async def test_objects_items_reference_are_kept_however_old(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client)
    item_id, original = await _image_item(client, storage, token)
    thumbnail = await _record_thumbnail(session, storage, user_id, item_id)
    file_response = await upload_file(client, storage, token, "doc.pdf", b"%PDF-1.4 content")
    assert file_response.status_code == 202, file_response.text
    _age_everything(storage)
    before = set(storage.uploads)

    result = await _reconciler(session, storage).reconcile()

    assert result.deleted == 0
    assert {original, thumbnail} <= set(storage.uploads)
    assert set(storage.uploads) == before


async def test_a_key_is_kept_whichever_row_holds_it(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """References are looked up by exact key in every key column, not
    inferred from the ids in the key: an object some row holds is never
    deleted, wherever it lives."""
    _, token = await register_and_login(client)
    item_id, _ = await _image_item(client, storage, token)
    elsewhere = _store(storage, f"users/{_USER}/images/{_ITEM}/{_OBJECT_ID}.png")
    await session.execute(
        update(ImageMetadata).where(ImageMetadata.item_id == item_id).values(storage_key=elsewhere)
    )
    await session.commit()

    await _reconciler(session, storage).reconcile()

    assert elsewhere in storage.uploads


# ---- Orphans: races and crashes ----


async def test_a_copy_left_by_a_finalize_that_died_before_its_commit_is_deleted_once_old(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, monkeypatch
):
    """The process dies after `CopyObject` but before the item is
    committed: nothing references the copy, and nothing ever will (a
    retried finalize copies again, to a fresh key)."""
    user_id, token = await register_and_login(client)
    started = await start_upload(
        client, token, type="image", content_type="image/png", size_bytes=len(_PNG_BYTES)
    )
    upload_id = started.json()["upload_id"]
    presigned = started.json()["upload"]
    storage.put(presigned["url"], presigned["headers"], _PNG_BYTES)

    async def crash(*args, **kwargs):
        raise RuntimeError("process died")

    with monkeypatch.context() as patch:
        patch.setattr(ItemRepository, "create_image_item", crash)
        with pytest.raises(RuntimeError):
            await finalize_upload(client, token, upload_id)
    [orphan] = [key for key in storage.uploads if key.startswith(f"users/{user_id}/images/{upload_id}/")]
    # The retry succeeds, with its own copy.
    assert (await finalize_upload(client, token, upload_id)).status_code == 202
    [kept] = [
        key
        for key in storage.uploads
        if key.startswith(f"users/{user_id}/images/{upload_id}/") and key != orphan
    ]
    reconciler = _reconciler(session, storage)

    # Recent: it could be a finalize about to commit; left alone.
    result = await reconciler.reconcile()
    assert result.deleted == 0 and result.too_recent >= 1
    assert orphan in storage.uploads

    _age_everything(storage)
    result = await reconciler.reconcile()

    assert result.deleted == 1
    assert orphan not in storage.uploads
    assert kept in storage.uploads
    item = (await client.get(f"/items/{upload_id}", headers=_auth(token))).json()
    assert item["status"] != "failed"


async def test_a_copy_left_by_a_finalize_that_died_and_was_never_retried_is_deleted(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, monkeypatch
):
    """The upload stays pending (abandoned): still, nothing will ever
    reference that copy."""
    user_id, token = await register_and_login(client)
    started = await start_upload(
        client, token, type="file", filename="doc.pdf", content_type="application/pdf", size_bytes=16
    )
    upload_id = started.json()["upload_id"]
    presigned = started.json()["upload"]
    storage.put(presigned["url"], presigned["headers"], b"%PDF-1.4 content")

    async def crash(*args, **kwargs):
        raise RuntimeError("process died")

    monkeypatch.setattr(ItemRepository, "create_file_item", crash)
    with pytest.raises(RuntimeError):
        await finalize_upload(client, token, upload_id)
    [orphan] = [key for key in storage.uploads if key.startswith(f"users/{user_id}/files/{upload_id}/")]
    _age_everything(storage)

    result = await _reconciler(session, storage).reconcile()

    assert result.deleted == 1
    assert orphan not in storage.uploads
    # Staging is the lifecycle rule's.
    assert any(key.startswith("uploads/") for key in storage.uploads)


async def test_a_thumbnail_stored_after_its_item_was_deleted_is_deleted_once_old(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """The worker stored the thumbnail, the item was deleted before the
    worker recorded it (so deleting the item didn't schedule it), and the
    worker died before deleting it itself."""
    user_id, token = await register_and_login(client)
    item_id, _ = await _image_item(client, storage, token)
    thumbnail = storage_keys.thumbnail_key(uuid.UUID(user_id), item_id)
    storage.uploads[thumbnail] = (b"webp", "image/webp")
    assert (await client.delete(f"/items/{item_id}", headers=_auth(token))).status_code == 204
    assert thumbnail in storage.uploads
    reconciler = _reconciler(session, storage)

    assert (await reconciler.reconcile()).deleted == 0
    _age(storage, thumbnail)
    assert (await reconciler.reconcile()).deleted == 1

    assert storage.uploads == {}


async def test_an_unrecorded_thumbnail_of_an_item_that_exists_is_kept(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """The worker stored it but hasn't recorded it yet (or died, and its
    job will be redelivered): the item may still come to reference it, at
    the same key, so it's never certain to be an orphan."""
    user_id, token = await register_and_login(client)
    item_id, _ = await _image_item(client, storage, token)
    thumbnail = _store(storage, storage_keys.thumbnail_key(uuid.UUID(user_id), item_id))

    result = await _reconciler(session, storage).reconcile()

    assert result.deleted == 0
    assert thumbnail in storage.uploads


async def test_objects_of_items_whose_deletion_was_lost_are_deleted(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """Items deleted before their deletions were scheduled (best effort,
    while storage was failing) left these for good; the scan finds them.
    (Here a deletion is still pending too: doing it twice is harmless.)"""
    user_id, token = await register_and_login(client)
    item_id, original = await _image_item(client, storage, token)
    thumbnail = await _record_thumbnail(session, storage, user_id, item_id)
    legacy = _store(storage, f"users/{user_id}/files/{uuid.uuid4()}.pdf")
    storage.fail_deletes = True
    assert (await client.delete(f"/items/{item_id}", headers=_auth(token))).status_code == 204
    storage.fail_deletes = False
    _age_everything(storage)

    result = await _reconciler(session, storage).reconcile()

    assert result.deleted == 3
    assert not {original, thumbnail, legacy} & set(storage.uploads)


async def test_recent_orphans_are_left_for_a_later_run(session: AsyncSession, storage: FakeObjectStorage):
    key = _store(storage, f"users/{_USER}/images/{_ITEM}/{_OBJECT_ID}.png", old=False)
    _age(storage, key, by=timedelta(hours=23))

    result = await _reconciler(session, storage).reconcile()

    assert result.deleted == 0 and result.too_recent == 1
    assert key in storage.uploads


def test_the_minimum_age_cant_be_set_too_low(session: AsyncSession, storage: FakeObjectStorage):
    with pytest.raises(ValueError):
        _reconciler(session, storage, min_age=timedelta(minutes=5))


# ---- Bounded runs ----


async def _cursor(session: AsyncSession) -> str | None:
    session.expire_all()
    return (
        await session.execute(
            select(storage_reconciliation.c.cursor).where(storage_reconciliation.c.prefix == PREFIX)
        )
    ).scalar_one()


async def test_a_scan_resumes_where_the_last_run_stopped(session: AsyncSession, storage: FakeObjectStorage):
    orphans = [_store(storage, f"users/{_USER}/images/{uuid.uuid4()}/{_OBJECT_ID}.png") for _ in range(5)]
    # One page per run.
    reconciler = _reconciler(session, storage, page_size=2, time_budget_seconds=0)

    first = await reconciler.reconcile()
    assert (first.scanned, first.deleted, first.pass_completed) == (2, 2, False)
    assert await _cursor(session) == sorted(orphans)[1]
    second = await reconciler.reconcile()
    assert (second.scanned, second.deleted, second.pass_completed) == (2, 2, False)
    third = await reconciler.reconcile()
    assert (third.scanned, third.deleted, third.pass_completed) == (1, 1, True)

    # The next pass starts over.
    assert await _cursor(session) is None
    assert storage.uploads == {}


async def test_a_run_stops_at_its_deletion_limit(
    session: AsyncSession, storage: FakeObjectStorage, caplog: pytest.LogCaptureFixture
):
    """Many orphans at once more likely means the database isn't the
    bucket's than that they're all orphans: deleting is slowed down, and
    someone is told."""
    orphans = sorted(_store(storage, f"users/{_USER}/images/{uuid.uuid4()}/{_OBJECT_ID}.png") for _ in range(5))
    reconciler = _reconciler(session, storage, max_deletions=2)

    with caplog.at_level(logging.ERROR):
        result = await reconciler.reconcile()

    assert result.deleted == 2
    assert set(storage.uploads) == set(orphans[2:])
    assert "deletion limit" in caplog.text
    # The next run carries on from there.
    assert await _cursor(session) == orphans[1]
    assert (await reconciler.reconcile()).deleted == 2
    assert (await reconciler.reconcile()).deleted == 1
    assert storage.uploads == {}


async def test_a_failed_deletion_is_retried_by_the_next_run(session: AsyncSession, storage: FakeObjectStorage):
    orphan = _store(storage, f"users/{_USER}/images/{_ITEM}/{_OBJECT_ID}.png")
    reconciler = _reconciler(session, storage)
    storage.fail_deletes = True

    with pytest.raises(ConnectionError):
        await reconciler.reconcile()

    assert orphan in storage.uploads
    storage.fail_deletes = False
    assert (await reconciler.reconcile()).deleted == 1
    assert orphan not in storage.uploads
