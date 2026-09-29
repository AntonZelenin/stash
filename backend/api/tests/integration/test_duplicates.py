from datetime import datetime
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import FileMetadata, Item

from conftest import FakeObjectStorage
from helpers import register_and_login, upload_file, upload_image

_PDF_BYTES = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"
# Same size as _PDF_BYTES, other content.
_SAME_SIZE_PDF_BYTES = b"%PDF-1.7\n2 0 obj << >> endobj\n%%EOF\n"
_LONGER_PDF_BYTES = b"%PDF-1.7\n10 0 obj << >> endobj\n%%EOF\n"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _file(filename: str, data: bytes, type: str = "file") -> dict:
    return {"type": type, "filename": filename, "size_bytes": len(data)}


async def _find(client: AsyncClient, token: str, *files: dict):
    return await client.post("/uploads/duplicates", json={"files": list(files)}, headers=_auth(token))


def _as_naive(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=None)


async def test_a_file_with_the_same_name_type_and_size_is_a_duplicate(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)
    first = await upload_file(client, storage, token, "report.pdf", _PDF_BYTES)
    last = await upload_file(client, storage, token, "report.pdf", _PDF_BYTES)

    response = await _find(client, token, _file("report.pdf", _PDF_BYTES))

    assert response.status_code == 200
    first_item = await session.get(Item, UUID(first.json()["id"]))
    last_item = await session.get(Item, UUID(last.json()["id"]))
    [group] = response.json()["duplicates"]
    assert group["type"] == "file"
    assert group["filename"] == "report.pdf"
    assert group["size_bytes"] == len(_PDF_BYTES)
    assert group["count"] == 2
    assert _as_naive(group["first_created_at"]) == first_item.created_at.replace(tzinfo=None)
    assert _as_naive(group["last_created_at"]) == last_item.created_at.replace(tzinfo=None)


async def test_only_metadata_is_compared_not_content(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    await upload_file(client, storage, token, "report.pdf", _PDF_BYTES)

    # Other content of the same size and name: still a candidate.
    same_size = await _find(client, token, _file("report.pdf", _SAME_SIZE_PDF_BYTES))
    assert same_size.json()["duplicates"][0]["count"] == 1


async def test_another_name_size_or_type_is_not_a_duplicate(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    await upload_file(client, storage, token, "report.pdf", _PDF_BYTES)

    response = await _find(
        client,
        token,
        # Same content, other name.
        _file("renamed.pdf", _PDF_BYTES),
        # Names are compared exactly.
        _file("Report.pdf", _PDF_BYTES),
        # Same name, other size.
        _file("report.pdf", _LONGER_PDF_BYTES),
        # Same name and size, uploaded as an image.
        _file("report.pdf", _PDF_BYTES, type="image"),
    )

    assert response.json() == {"duplicates": []}


async def test_images_match_by_their_kept_filename(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    await upload_image(client, storage, token, _PNG_BYTES, filename="photo.png")

    response = await _find(client, token, _file("photo.png", _PNG_BYTES, type="image"), _file("x.png", _PNG_BYTES))

    [group] = response.json()["duplicates"]
    assert (group["type"], group["filename"], group["count"]) == ("image", "photo.png", 1)


async def test_the_name_is_compared_as_it_would_be_stored(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    await upload_file(client, storage, token, "report.pdf", _PDF_BYTES)

    response = await _find(client, token, _file("C:\\docs\\report.pdf", _PDF_BYTES))

    [group] = response.json()["duplicates"]
    # Returned under the name as asked, so the client can find its file.
    assert group["filename"] == "C:\\docs\\report.pdf"


async def test_a_duplicate_upload_is_a_new_item_with_its_own_name(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)
    first = await upload_file(client, storage, token, "report.pdf", _PDF_BYTES)

    again = await upload_file(client, storage, token, "report.pdf", _PDF_BYTES)

    assert again.status_code == 202
    assert again.json()["id"] != first.json()["id"]
    original = await session.get(FileMetadata, UUID(first.json()["id"]))
    copy = await session.get(FileMetadata, UUID(again.json()["id"]))
    assert copy.filename == original.filename == "report.pdf"
    assert copy.storage_key != original.storage_key


async def test_other_users_items_are_not_duplicates(client: AsyncClient, storage: FakeObjectStorage):
    _, alice = await register_and_login(client)
    await upload_file(client, storage, alice, "report.pdf", _PDF_BYTES)
    _, bob = await register_and_login(client, email="bob@example.com")

    response = await _find(client, bob, _file("report.pdf", _PDF_BYTES))

    assert response.json() == {"duplicates": []}


async def test_deleted_items_are_not_duplicates(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    created = await upload_file(client, storage, token, "report.pdf", _PDF_BYTES)
    await client.delete(f"/items/{created.json()['id']}", headers=_auth(token))

    response = await _find(client, token, _file("report.pdf", _PDF_BYTES))

    assert response.json() == {"duplicates": []}


async def test_invalid_requests_are_rejected(client: AsyncClient):
    _, token = await register_and_login(client)
    valid = _file("report.pdf", _PDF_BYTES)

    assert (await _find(client, token)).status_code == 422
    assert (await _find(client, token, {**valid, "filename": ""})).status_code == 422
    assert (await _find(client, token, {**valid, "size_bytes": -1})).status_code == 422
    assert (await _find(client, token, {**valid, "type": "text"})).status_code == 422
    assert (await _find(client, token, {**valid, "sha256": "ab"})).status_code == 422
    assert (await _find(client, token, *[valid] * 101)).status_code == 422
    assert (await client.post("/uploads/duplicates", json={"files": [valid]})).status_code == 401
