"""`GET /items/{id}/video-url`: a fresh, long-lived playback URL for a
video file item, issued only to its owner."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import FileMetadata
from conftest import FakeObjectStorage
from helpers import register_and_login, upload_file

# Just enough of each format for the content check to recognize it.
_MP4_BYTES = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32
_MKV_BYTES = b"\x1a\x45\xdf\xa3" + b"\x00" * 32
_PDF_BYTES = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"
_PLAYBACK_TTL_SECONDS = 4 * 3600


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _upload(client: AsyncClient, storage: FakeObjectStorage, token: str, filename: str, data: bytes) -> UUID:
    response = await upload_file(client, storage, token, filename, data)
    assert response.status_code == 202
    return UUID(response.json()["id"])


async def test_owner_gets_a_long_lived_playback_url(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)
    item_id = await _upload(client, storage, token, "Holiday.mp4", _MP4_BYTES)
    key = (await session.get(FileMetadata, item_id)).storage_key

    before = datetime.now(UTC)
    response = await client.get(f"/items/{item_id}/video-url", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["url"].startswith(f"https://fake-storage.test/{key}?")
    # Hours, not the listing URLs' one hour, and its expiry says so.
    assert f"expires_in={_PLAYBACK_TTL_SECONDS}" in body["url"]
    expires_at = datetime.fromisoformat(body["expires_at"])
    ttl = timedelta(seconds=_PLAYBACK_TTL_SECONDS)
    assert before + ttl <= expires_at <= datetime.now(UTC) + ttl
    # For the player: the validated type, no download filename.
    assert storage.download_content_types[key] == "video/mp4"
    assert "filename=" not in body["url"]


async def test_formats_browsers_may_not_play_still_get_a_url(
    client: AsyncClient, storage: FakeObjectStorage
):
    """Whether it plays is up to the browser; the server doesn't guess."""
    _, token = await register_and_login(client)
    item_id = await _upload(client, storage, token, "raw.mkv", _MKV_BYTES)

    response = await client.get(f"/items/{item_id}/video-url", headers=_auth(token))

    assert response.status_code == 200


async def test_each_request_issues_a_fresh_url(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    item_id = await _upload(client, storage, token, "clip.mp4", _MP4_BYTES)

    first = (await client.get(f"/items/{item_id}/video-url", headers=_auth(token))).json()
    second = (await client.get(f"/items/{item_id}/video-url", headers=_auth(token))).json()

    assert second["expires_at"] >= first["expires_at"]


async def test_non_video_items_have_no_playback_url(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    pdf_id = await _upload(client, storage, token, "report.pdf", _PDF_BYTES)
    note = await client.post("/items/text", json={"text": "hello"}, headers=_auth(token))

    for item_id in (pdf_id, note.json()["id"], uuid4()):
        response = await client.get(f"/items/{item_id}/video-url", headers=_auth(token))
        assert response.status_code == 404, item_id


async def test_other_users_video_is_indistinguishable_from_a_missing_one(
    client: AsyncClient, storage: FakeObjectStorage
):
    _, owner_token = await register_and_login(client, email="owner@example.com")
    _, other_token = await register_and_login(client, email="other@example.com")
    item_id = await _upload(client, storage, owner_token, "clip.mp4", _MP4_BYTES)

    response = await client.get(f"/items/{item_id}/video-url", headers=_auth(other_token))

    assert response.status_code == 404
    assert "fake-storage" not in response.text


async def test_playback_url_requires_authentication(client: AsyncClient):
    assert (await client.get(f"/items/{uuid4()}/video-url")).status_code == 401
