"""The direct-to-storage upload flow itself: start (authorize) → the client
uploads to storage → finalize. What's specific to images or files is in
their own test modules."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import FileMetadata, Item, PendingUpload
from conftest import FakeJobQueue, FakeObjectStorage
from helpers import finalize_upload, register_and_login, start_upload, upload_file

_PDF_BYTES = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _start_pdf(client: AsyncClient, token: str, **fields):
    response = await start_upload(
        client, token, type="file", filename="report.pdf", size_bytes=len(_PDF_BYTES), **fields
    )
    assert response.status_code == 201, response.text
    return response.json()


def _put(storage: FakeObjectStorage, started: dict, data: bytes = _PDF_BYTES) -> None:
    storage.put(started["upload"]["url"], started["upload"]["headers"], data)


async def _item_count(session: AsyncSession) -> int:
    return len((await session.execute(Item.__table__.select())).all())


async def test_start_upload_records_a_pending_upload_that_expires(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client)
    before = datetime.now(UTC)

    started = await _start_pdf(client, token, text="notes", tags=["Work"])

    pending = await session.get(PendingUpload, UUID(started["upload_id"]))
    assert str(pending.user_id) == user_id
    assert pending.storage_key == f"users/{user_id}/files/{started['upload_id']}.pdf"
    assert (pending.filename, pending.caption, pending.tag_names) == ("report.pdf", "notes", ["Work"])
    assert pending.size_bytes == len(_PDF_BYTES)
    # Short-lived: the URL's TTL (UPLOAD_URL_TTL_SECONDS, 15 min by default).
    expires_at = datetime.fromisoformat(started["expires_at"])
    assert before + timedelta(minutes=14) < expires_at < before + timedelta(minutes=16)


@pytest.mark.parametrize("field", ["storage_key", "key", "upload_id", "item_id", "user_id"])
async def test_client_cannot_choose_the_storage_key_or_ids(
    client: AsyncClient, storage: FakeObjectStorage, field: str
):
    _, token = await register_and_login(client)

    response = await start_upload(
        client, token, type="file", filename="a.pdf", size_bytes=10, **{field: "users/someone-else/files/x.pdf"}
    )

    assert response.status_code == 422
    assert storage.signed_uploads == {}


async def test_filename_never_reaches_the_storage_key(client: AsyncClient, storage: FakeObjectStorage):
    user_id, token = await register_and_login(client)
    other_user = uuid4()

    started = (
        await start_upload(
            client, token, type="file", filename=f"../../users/{other_user}/files/x.pdf", size_bytes=10
        )
    ).json()

    assert list(storage.signed_uploads) == [f"users/{user_id}/files/{started['upload_id']}.pdf"]


async def test_invalid_metadata_gets_no_upload_url(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)

    bad_tags = await start_upload(client, token, type="file", filename="a.pdf", size_bytes=10, tags=[" "])
    bad_type = await start_upload(client, token, type="text", filename="a.txt", size_bytes=10)
    negative_size = await start_upload(client, token, type="file", filename="a.pdf", size_bytes=-1)

    assert (bad_tags.status_code, bad_type.status_code, negative_size.status_code) == (422, 422, 422)
    assert storage.signed_uploads == {}


async def test_finalize_requires_token(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    started = await _start_pdf(client, token)
    _put(storage, started)

    response = await client.post(f"/uploads/{started['upload_id']}/finalize")

    assert response.status_code == 401


async def test_finalize_by_another_user_is_rejected(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, document_queue: FakeJobQueue
):
    """Someone else's upload is indistinguishable from a missing one, and
    stays the owner's to finalize."""
    _, alice_token = await register_and_login(client, email="alice@example.com")
    _, bob_token = await register_and_login(client, email="bob@example.com")
    started = await _start_pdf(client, alice_token)
    _put(storage, started)

    response = await finalize_upload(client, bob_token, started["upload_id"])

    assert response.status_code == 404
    assert await _item_count(session) == 0
    assert document_queue.published == []
    assert storage.inspected == []

    assert (await finalize_upload(client, alice_token, started["upload_id"])).status_code == 202
    assert (await client.get("/items", headers=_auth(bob_token))).json()["items"] == []


async def test_finalize_by_another_user_after_it_was_finalized_is_rejected(
    client: AsyncClient, storage: FakeObjectStorage
):
    _, alice_token = await register_and_login(client, email="alice@example.com")
    _, bob_token = await register_and_login(client, email="bob@example.com")
    created = await upload_file(client, storage, alice_token, "report.pdf", _PDF_BYTES)

    response = await finalize_upload(client, bob_token, created.json()["id"])

    assert response.status_code == 404


async def test_finalize_unknown_upload(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await finalize_upload(client, token, str(uuid4()))

    assert response.status_code == 404


async def test_finalize_before_the_upload_arrived_creates_nothing_and_can_be_retried(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, document_queue: FakeJobQueue
):
    _, token = await register_and_login(client)
    started = await _start_pdf(client, token)

    response = await finalize_upload(client, token, started["upload_id"])

    assert response.status_code == 409
    assert await _item_count(session) == 0
    assert document_queue.published == []
    # Still pending: uploading and finalizing again works.
    assert await session.get(PendingUpload, UUID(started["upload_id"])) is not None

    _put(storage, started)
    response = await finalize_upload(client, token, started["upload_id"])

    assert response.status_code == 202
    assert response.json()["id"] == started["upload_id"]


async def test_upload_that_is_never_finalized_is_not_an_item(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, document_queue: FakeJobQueue
):
    """Uploaded to storage, but the client never finalized (e.g. the tab
    was closed): the object is there, but it isn't an item — not listed,
    not processed — only a pending upload, which is how a future cleanup
    finds it."""
    _, token = await register_and_login(client)
    started = await _start_pdf(client, token)
    _put(storage, started)

    listed = (await client.get("/items", headers=_auth(token))).json()["items"]

    assert listed == []
    assert await _item_count(session) == 0
    assert document_queue.published == []
    pending = await session.get(PendingUpload, UUID(started["upload_id"]))
    assert pending.storage_key in storage.uploads


async def test_finalize_moves_the_upload_from_pending_to_item(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    """The item takes the id and storage key the upload started with."""
    _, token = await register_and_login(client)
    started = await _start_pdf(client, token)
    _put(storage, started)
    upload_id = UUID(started["upload_id"])
    signed_key = (await session.get(PendingUpload, upload_id)).storage_key

    await finalize_upload(client, token, started["upload_id"])

    session.expunge_all()
    assert await session.get(PendingUpload, upload_id) is None
    assert (await session.get(FileMetadata, upload_id)).storage_key == signed_key


async def test_finalize_is_idempotent(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, document_queue: FakeJobQueue
):
    """A retried finalize (e.g. its response was lost) returns the same
    item, and doesn't process it twice."""
    _, token = await register_and_login(client)
    started = await _start_pdf(client, token)
    _put(storage, started)

    first = await finalize_upload(client, token, started["upload_id"])
    second = await finalize_upload(client, token, started["upload_id"])

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["id"] == second.json()["id"] == started["upload_id"]
    assert await _item_count(session) == 1
    assert len(document_queue.published) == 1


async def test_finalize_starts_processing_through_the_outbox(
    client: AsyncClient, storage: FakeObjectStorage, document_queue: FakeJobQueue, embedding_queue: FakeJobQueue
):
    _, token = await register_and_login(client)
    started = await _start_pdf(client, token, text="caption")
    _put(storage, started)

    await finalize_upload(client, token, started["upload_id"])

    [analysis] = document_queue.published
    [embedding] = embedding_queue.published
    assert str(analysis.item_id) == str(embedding.item_id) == started["upload_id"]


async def test_rejected_content_discards_the_upload(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)
    started = (
        await start_upload(client, token, type="image", content_type="image/png", size_bytes=4)
    ).json()
    _put(storage, started, b"text")

    response = await finalize_upload(client, token, started["upload_id"])

    assert response.status_code == 422
    assert storage.uploads == {}
    assert await session.get(PendingUpload, UUID(started["upload_id"])) is None
    # Gone for good: finalizing again doesn't find it.
    assert (await finalize_upload(client, token, started["upload_id"])).status_code == 404


async def test_text_item_id_is_not_an_upload(client: AsyncClient):
    _, token = await register_and_login(client)
    note = await client.post("/items/text", json={"text": "hi"}, headers=_auth(token))

    response = await finalize_upload(client, token, note.json()["id"])

    assert response.status_code == 404
