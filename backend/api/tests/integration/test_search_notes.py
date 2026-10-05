"""An item's search note: the user's extra context for search, kept apart
from the caption (or a note's text) and the generated description. Its
full-text matching is in `tests/postgres/test_search_postgres.py`."""

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Description, Item, ItemStatus, ItemType, PendingUpload, TextContent

from conftest import FakeJobQueue, FakeObjectStorage
from helpers import register_and_login, start_upload, upload_file, upload_image

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5a1e8b10000000049454e44ae426082"
)
_PDF_BYTES = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _get(client: AsyncClient, token: str, item_id: str) -> dict:
    response = await client.get(f"/items/{item_id}", headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()


async def _listed(client: AsyncClient, token: str) -> dict[str, dict]:
    response = await client.get("/items", headers=_auth(token))
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()["items"]}


async def _stored_note(session: AsyncSession, item_id: str) -> str | None:
    return (await session.execute(select(Item.search_note).where(Item.id == UUID(item_id)))).scalar_one()


async def _description(session: AsyncSession, item_id: str) -> str | None:
    return (
        await session.execute(select(Description.text).where(Description.item_id == UUID(item_id)))
    ).scalar_one_or_none()


# ---- creating ----


async def test_a_note_without_a_search_note_has_none(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    created = await client.post("/items/text", json={"text": "buy milk"}, headers=_auth(token))

    item_id = created.json()["id"]
    assert (await _get(client, token, item_id))["search_note"] is None
    assert await _stored_note(session, item_id) is None


async def test_a_note_is_saved_with_its_search_note_apart_from_its_text(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    created = await client.post(
        "/items/text", json={"text": "buy milk", "search_note": "  groceries for Sunday  "}, headers=_auth(token)
    )

    assert created.status_code == 202, created.text
    item_id = created.json()["id"]
    body = await _get(client, token, item_id)
    assert (body["text"], body["search_note"]) == ("buy milk", "groceries for Sunday")
    assert await _stored_note(session, item_id) == "groceries for Sunday"
    # Not part of what's embedded or shown as the note.
    assert await _description(session, item_id) == "buy milk"


async def test_a_blank_search_note_is_none(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    created = await client.post("/items/text", json={"text": "buy milk", "search_note": "   "}, headers=_auth(token))

    assert await _stored_note(session, created.json()["id"]) is None


async def test_an_image_is_uploaded_with_its_search_note_apart_from_its_caption(
    client: AsyncClient, storage: FakeObjectStorage, session: AsyncSession
):
    _, token = await register_and_login(client)
    response = await upload_image(
        client, storage, token, _PNG_BYTES, text="Granny's kitchen", search_note="borscht recipe, 1987"
    )

    assert response.status_code == 202, response.text
    item_id = response.json()["id"]
    body = await _get(client, token, item_id)
    assert (body["text"], body["search_note"]) == ("Granny's kitchen", "borscht recipe, 1987")
    # The description (what's embedded) is the caption alone.
    assert await _description(session, item_id) == "Granny's kitchen"


async def test_a_file_is_uploaded_with_a_search_note_and_no_caption(
    client: AsyncClient, storage: FakeObjectStorage, session: AsyncSession, embedding_queue: FakeJobQueue
):
    _, token = await register_and_login(client)
    response = await upload_file(client, storage, token, "report.pdf", _PDF_BYTES, search_note="tax return 2025")

    item_id = response.json()["id"]
    body = await _get(client, token, item_id)
    assert (body["text"], body["search_note"]) == (None, "tax return 2025")
    # Nothing to embed without a caption: a search note never is.
    assert await _description(session, item_id) is None
    assert embedding_queue.published == []


async def test_uploads_without_a_search_note_have_none(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    image = await upload_image(client, storage, token, _PNG_BYTES, text="caption")
    file = await upload_file(client, storage, token, "report.pdf", _PDF_BYTES)

    listed = await _listed(client, token)
    assert listed[image.json()["id"]]["search_note"] is None
    assert listed[file.json()["id"]]["search_note"] is None


async def test_a_started_upload_keeps_its_search_note_until_finalized(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    started = await start_upload(
        client, token, type="file", filename="a.txt", size_bytes=1, search_note="  meeting notes  "
    )

    assert started.status_code == 201, started.text
    pending = await session.get(PendingUpload, UUID(started.json()["upload_id"]))
    assert pending.search_note == "meeting notes"


# ---- editing ----


async def test_a_search_note_can_be_added_changed_and_cleared(
    client: AsyncClient, storage: FakeObjectStorage, session: AsyncSession, embedding_queue: FakeJobQueue
):
    _, token = await register_and_login(client)
    item_id = (await upload_image(client, storage, token, _PNG_BYTES, text="caption")).json()["id"]
    embedding_queue.published.clear()

    added = await client.patch(f"/items/{item_id}", json={"search_note": "first"}, headers=_auth(token))
    assert added.status_code == 200, added.text
    assert added.json()["search_note"] == "first"
    changed = await client.patch(f"/items/{item_id}", json={"search_note": "  second  "}, headers=_auth(token))
    assert changed.json()["search_note"] == "second"
    assert await _stored_note(session, item_id) == "second"

    cleared = await client.patch(f"/items/{item_id}", json={"search_note": ""}, headers=_auth(token))
    assert cleared.json()["search_note"] is None
    assert await _stored_note(session, item_id) is None
    # The caption and description were never touched, nor re-embedded.
    assert cleared.json()["text"] == "caption"
    assert await _description(session, item_id) == "caption"
    assert embedding_queue.published == []


async def test_editing_other_fields_keeps_the_search_note(client: AsyncClient, embedding_queue: FakeJobQueue):
    _, token = await register_and_login(client)
    created = await client.post("/items/text", json={"text": "buy milk", "search_note": "groceries"}, headers=_auth(token))
    item_id = created.json()["id"]

    edited = await client.patch(f"/items/{item_id}", json={"text": "buy oat milk"}, headers=_auth(token))

    assert (edited.json()["text"], edited.json()["search_note"]) == ("buy oat milk", "groceries")


async def test_search_note_and_caption_are_edited_together(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    item_id = (await upload_file(client, storage, token, "report.pdf", _PDF_BYTES)).json()["id"]

    edited = await client.patch(
        f"/items/{item_id}", json={"text": "Q3 report", "search_note": "board meeting"}, headers=_auth(token)
    )

    assert (edited.json()["text"], edited.json()["search_note"]) == ("Q3 report", "board meeting")


async def test_another_users_search_note_cannot_be_edited(client: AsyncClient):
    _, owner = await register_and_login(client, email="owner@example.com")
    _, other = await register_and_login(client, email="other@example.com")
    item_id = (await client.post("/items/text", json={"text": "mine"}, headers=_auth(owner))).json()["id"]

    response = await client.patch(f"/items/{item_id}", json={"search_note": "theirs"}, headers=_auth(other))

    assert response.status_code == 404
    assert (await _get(client, owner, item_id))["search_note"] is None


# ---- existing items ----


async def test_items_saved_before_search_notes_existed_still_list_and_edit(
    client: AsyncClient, session: AsyncSession
):
    """Rows as they were before the column: no search note."""
    user_id, token = await register_and_login(client)
    legacy = Item(
        user_id=UUID(str(user_id)),
        type=ItemType.text,
        status=ItemStatus.completed,
        text_content=TextContent(text="old note"),
        description=Description(text="old note"),
    )
    session.add(legacy)
    await session.commit()
    item_id = str(legacy.id)

    assert (await _listed(client, token))[item_id]["search_note"] is None
    edited = await client.patch(f"/items/{item_id}", json={"text": "old note, edited"}, headers=_auth(token))
    assert (edited.json()["text"], edited.json()["search_note"]) == ("old note, edited", None)
    noted = await client.patch(f"/items/{item_id}", json={"search_note": "from 2024"}, headers=_auth(token))
    assert noted.json()["search_note"] == "from 2024"
