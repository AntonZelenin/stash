from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.items import services
from app.items.models import Description, FileMetadata, Item, ItemType, TextContent
from stash_shared.queue.base import FileRef
from stash_shared.queue.base import ItemType as QueueItemType

from conftest import FakeJobQueue, FakeObjectStorage
from helpers import register_and_login, start_upload, upload_file

_PDF_BYTES = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"
_DOCX_BYTES = b"PK\x03\x04" + b"\x00" * 64  # zip container header, as a .docx starts


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_upload_pdf_stores_file_and_metadata(
    client: AsyncClient,
    session: AsyncSession,
    storage: FakeObjectStorage,
    queue: FakeJobQueue,
    document_queue: FakeJobQueue,
):
    user_id, token = await register_and_login(client)

    response = await upload_file(client, storage, token, "Quarterly Report.pdf", _PDF_BYTES)

    assert response.status_code == 202
    # PDFs are analyzable: pending until the document analyzer is done,
    # and sent to its queue, not the image pipeline's.
    assert response.json()["status"] == "pending"
    assert queue.published == []
    [job] = document_queue.published
    assert str(job.item_id) == response.json()["id"]
    assert job.item_type == QueueItemType.file
    assert job.image is None
    assert job.file == FileRef(
        storage_key=f"users/{user_id}/files/{job.item_id}.pdf",
        content_type="application/pdf",
        filename="Quarterly Report.pdf",
    )

    item = await session.get(Item, UUID(response.json()["id"]))
    assert item.type == ItemType.file
    assert str(item.user_id) == user_id
    stored = await session.get(FileMetadata, item.id)
    assert stored.filename == "Quarterly Report.pdf"
    assert stored.content_type == "application/pdf"
    assert stored.size_bytes == len(_PDF_BYTES)
    assert stored.storage_key == f"users/{user_id}/files/{item.id}.pdf"
    assert storage.uploads[stored.storage_key] == (_PDF_BYTES, "application/pdf")


async def test_start_upload_takes_the_type_from_the_filename_not_the_client(
    client: AsyncClient, storage: FakeObjectStorage
):
    user_id, token = await register_and_login(client)

    response = await start_upload(
        client, token, type="file", filename="report.PDF", content_type="text/html", size_bytes=len(_PDF_BYTES)
    )

    assert response.status_code == 201
    key = f"users/{user_id}/files/{response.json()['upload_id']}.pdf"
    assert storage.signed_uploads == {key: ("application/pdf", len(_PDF_BYTES))}
    assert response.json()["upload"]["headers"] == {"Content-Type": "application/pdf"}


async def test_listed_file_has_metadata_and_download_url(client: AsyncClient, storage: FakeObjectStorage):
    user_id, token = await register_and_login(client)
    created = await upload_file(client, storage, token, "Quarterly Report.pdf", _PDF_BYTES, text="for the board")

    [listed] = (await client.get("/items", headers=_auth(token))).json()["items"]

    assert listed["id"] == created.json()["id"]
    assert listed["type"] == "file"
    assert listed["text"] == "for the board"
    assert listed["file"] == {
        "filename": "Quarterly Report.pdf",
        "content_type": "application/pdf",
        "size_bytes": len(_PDF_BYTES),
    }
    # Signed with the original filename, so it opens/saves under that name.
    assert listed["download_url"] == (
        f"https://fake-storage.test/users/{user_id}/files/{listed['id']}.pdf"
        "?expires_in=3600&filename=Quarterly Report.pdf&disposition=inline"
    )
    assert listed["thumbnail_url"] is None


async def test_caption_is_stored_and_searchable_source(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)

    response = await upload_file(client, storage, token, "plan.pdf", _PDF_BYTES, text="  roadmap draft  ")

    item_id = UUID(response.json()["id"])
    assert (await session.get(TextContent, item_id)).text == "roadmap draft"
    assert (await session.get(Description, item_id)).text == "roadmap draft"


@pytest.mark.parametrize(
    ("filename", "data", "content_type"),
    [
        ("notes.docx", _DOCX_BYTES, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "application/msword"),
        ("letter.rtf", b"{\\rtf1\\ansi hello}", "application/rtf"),
        ("todo.TXT", "купить молоко\n".encode(), "text/plain; charset=utf-8"),
        ("data.csv", b"a,b\n1,2\n", "text/csv; charset=utf-8"),
        ("readme.md", b"# Title\n", "text/markdown; charset=utf-8"),
    ],
)
async def test_supported_types(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, filename, data, content_type
):
    _, token = await register_and_login(client)

    response = await upload_file(client, storage, token, filename, data)

    assert response.status_code == 202, response.text
    stored = await session.get(FileMetadata, UUID(response.json()["id"]))
    assert stored.content_type == content_type


async def test_downloads_are_served_with_the_validated_content_type(client: AsyncClient, storage: FakeObjectStorage):
    """The object was stored as the extension's type ("text/plain") before
    its content was checked; downloads use the validated one, charset
    included."""
    _, token = await register_and_login(client)
    await upload_file(client, storage, token, "todo.txt", "купить молоко\n".encode())

    [listed] = (await client.get("/items", headers=_auth(token))).json()["items"]

    [(key, (_, stored_as))] = storage.uploads.items()
    assert stored_as == "text/plain"
    assert storage.download_content_types[key] == "text/plain; charset=utf-8"
    assert listed["file"]["content_type"] == "text/plain; charset=utf-8"


@pytest.mark.parametrize(
    ("filename", "data", "extension", "stored_as"),
    [
        ("program.exe", b"MZ\x90\x00", "", "application/octet-stream"),  # unrecognized extension
        # An image type the image pipeline doesn't take.
        ("photo.heic", b"\x00\x00\x00\x18ftypheic", "", "application/octet-stream"),
        ("no-extension", _PDF_BYTES, "", "application/octet-stream"),
        # Content doesn't match the extension. The key's extension was picked
        # from the filename before the content existed, so it stays.
        ("fake.pdf", b"<html>not a pdf</html>", ".pdf", "application/pdf"),
        ("binary.txt", b"abc\x00\x01\x02", ".txt", "text/plain"),
    ],
)
async def test_other_files_are_stored_as_generic_downloads(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, filename, data, extension, stored_as
):
    """Anything is accepted; what isn't a recognized format is kept as an
    opaque binary that can only be downloaded, never displayed inline."""
    user_id, token = await register_and_login(client)

    response = await upload_file(client, storage, token, filename, data)

    assert response.status_code == 202
    item_id = UUID(response.json()["id"])
    stored = await session.get(FileMetadata, item_id)
    assert stored.filename == filename
    assert stored.content_type == "application/octet-stream"
    assert stored.storage_key == f"users/{user_id}/files/{item_id}{extension}"
    assert storage.uploads[stored.storage_key] == (data, stored_as)

    [listed] = (await client.get("/items", headers=_auth(token))).json()["items"]
    assert listed["download_url"].endswith(f"&filename={filename}&disposition=attachment")
    # Served as generic whatever it was stored as.
    assert storage.download_content_types[stored.storage_key] == "application/octet-stream"


async def test_html_is_recognized_but_never_displayed_inline(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    await upload_file(client, storage, token, "page.html", b"<script>alert(1)</script>")

    [listed] = (await client.get("/items", headers=_auth(token))).json()["items"]

    assert listed["file"]["content_type"] == "text/html; charset=utf-8"
    assert listed["download_url"].endswith("&disposition=attachment")


async def test_rejects_empty_file(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)

    response = await upload_file(client, storage, token, "empty.pdf", b"")

    assert response.status_code == 422
    assert response.json()["detail"] == "File is empty"
    assert storage.signed_uploads == {}


async def test_rejects_file_over_size_limit(client: AsyncClient, storage: FakeObjectStorage, monkeypatch):
    # Lowered so the test needn't allocate 50 MB; the check is the same.
    monkeypatch.setattr(services, "_MAX_FILE_SIZE_BYTES", len(_PDF_BYTES) - 1)
    _, token = await register_and_login(client)

    response = await upload_file(client, storage, token, "big.pdf", _PDF_BYTES)

    assert response.status_code == 422
    assert response.json()["detail"] == "File is too large (max 50 MB)"
    assert storage.signed_uploads == {}


def test_size_limit_is_50_mb():
    assert services._MAX_FILE_SIZE_BYTES == 50 * 1024 * 1024


async def test_path_in_filename_is_dropped(client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage):
    _, token = await register_and_login(client)

    response = await upload_file(client, storage, token, "C:\\Users\\me\\Desktop\\report.pdf", _PDF_BYTES)

    stored = await session.get(FileMetadata, UUID(response.json()["id"]))
    assert stored.filename == "report.pdf"


async def test_upload_requires_token(client: AsyncClient):
    response = await client.post("/uploads", json={"type": "file", "filename": "a.pdf", "size_bytes": 10})

    assert response.status_code == 401


async def test_delete_file_item_removes_stored_file(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    created = await upload_file(client, storage, token, "report.pdf", _PDF_BYTES)

    response = await client.delete(f"/items/{created.json()['id']}", headers=_auth(token))

    assert response.status_code == 204
    assert storage.uploads == {}


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("setup.exe", b"MZ\x90\x00"),  # unrecognized
        ("book.mobi", b"\x00" * 60 + b"BOOKMOBI" + b"\x00" * 16),  # recognized, not analyzable
        ("stuff.zip", b"PK\x03\x04" + b"\x00" * 16),
    ],
)
async def test_non_analyzable_files_are_completed_and_not_queued(
    client: AsyncClient, storage: FakeObjectStorage, document_queue: FakeJobQueue, filename, data
):
    _, token = await register_and_login(client)

    response = await upload_file(client, storage, token, filename, data)

    assert response.status_code == 202
    assert response.json()["status"] == "completed"
    assert document_queue.published == []


async def test_analyzable_file_whose_job_cannot_be_published_yet_stays_pending_until_a_later_request(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage, document_queue: FakeJobQueue
):
    """Same as images: its job waits in the outbox for the next flush."""
    document_queue.fail_publish = True
    _, token = await register_and_login(client)

    response = await upload_file(client, storage, token, "notes.txt", b"hello")

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    item_id = UUID(response.json()["id"])
    item = await session.get(Item, item_id)
    assert item.status == "pending"
    assert document_queue.published == []

    document_queue.fail_publish = False
    await upload_file(client, storage, token, "other.bin", b"\x00\x01")

    [job] = document_queue.published
    assert job.item_id == item_id
