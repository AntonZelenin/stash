from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.queue.base import ItemType as QueueItemType

from app.items.models import Description, Embedding, FileMetadata, TextContent

from conftest import FakeJobQueue, FakeObjectStorage
from helpers import register_and_login

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5a1e8b10000000049454e44ae426082"
)
_PDF_BYTES = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _note(client: AsyncClient, token: str, text: str) -> str:
    return (await client.post("/items/text", json={"text": text}, headers=_auth(token))).json()["id"]


async def _image(client: AsyncClient, token: str, **form) -> str:
    response = await client.post(
        "/items/image", files={"file": ("photo.png", _PNG_BYTES, "image/png")}, data=form, headers=_auth(token)
    )
    return response.json()["id"]


async def _file(client: AsyncClient, token: str, filename: str = "report.pdf", **form) -> str:
    response = await client.post(
        "/items/file", files={"file": (filename, _PDF_BYTES, "application/pdf")}, data=form, headers=_auth(token)
    )
    return response.json()["id"]


async def _edit(client: AsyncClient, token: str, item_id: str, **fields):
    return await client.patch(f"/items/{item_id}", json=fields, headers=_auth(token))


async def _description(session: AsyncSession, item_id: str) -> str | None:
    return (
        await session.execute(select(Description.text).where(Description.item_id == UUID(item_id)))
    ).scalar_one_or_none()


async def _set_description(session: AsyncSession, item_id: str, text: str) -> None:
    """Stands in for the content analyzer having completed the item."""
    row = await session.get(Description, UUID(item_id))
    if row is None:
        session.add(Description(item_id=UUID(item_id), text=text))
    else:
        row.text = text
    await session.commit()


async def test_edit_note_text(client: AsyncClient, session: AsyncSession, embedding_queue: FakeJobQueue):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "buy milk")
    embedding_queue.published.clear()

    response = await _edit(client, token, item_id, text="  buy oat milk  ")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == item_id
    assert body["type"] == "text"
    assert body["text"] == "buy oat milk"
    assert await _description(session, item_id) == "buy oat milk"
    [job] = embedding_queue.published
    assert job.item_id == UUID(item_id)


async def test_editing_text_into_a_url_makes_it_a_link_with_the_same_id(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "see later")

    body = (await _edit(client, token, item_id, text="https://example.com/a")).json()

    assert (body["id"], body["type"], body["text"]) == (item_id, "link", "https://example.com/a")
    [listed] = (await client.get("/items", headers=_auth(token))).json()["items"]
    assert (listed["id"], listed["type"]) == (item_id, "link")


async def test_editing_a_link_into_prose_makes_it_a_note(client: AsyncClient, embedding_queue: FakeJobQueue):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "https://example.com")
    embedding_queue.published.clear()

    body = (await _edit(client, token, item_id, text="https://example.com is worth a look")).json()

    assert (body["id"], body["type"]) == (item_id, "text")
    [job] = embedding_queue.published
    assert job.item_type == QueueItemType.text


async def test_unchanged_text_is_not_re_embedded(client: AsyncClient, embedding_queue: FakeJobQueue):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "same")
    embedding_queue.published.clear()

    assert (await _edit(client, token, item_id, text="same")).status_code == 200
    assert embedding_queue.published == []


async def test_note_text_cannot_be_emptied(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "keep")

    response = await _edit(client, token, item_id, text="   ")

    assert response.status_code == 422
    assert response.json()["detail"] == "Text must not be empty"


async def test_edit_image_caption_keeps_the_generated_description(
    client: AsyncClient, session: AsyncSession, embedding_queue: FakeJobQueue
):
    _, token = await register_and_login(client)
    item_id = await _image(client, token, text="our cat")
    await _set_description(session, item_id, "our cat\n\nA grey cat asleep on a sofa.")
    embedding_queue.published.clear()

    body = (await _edit(client, token, item_id, text="Tom on the sofa")).json()

    assert (body["type"], body["text"]) == ("image", "Tom on the sofa")
    session.expire_all()
    assert await _description(session, item_id) == "Tom on the sofa\n\nA grey cat asleep on a sofa."
    [job] = embedding_queue.published
    assert job.item_type == QueueItemType.image


async def test_add_caption_to_uncaptioned_analyzed_image(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    item_id = await _image(client, token)
    await _set_description(session, item_id, "A grey cat.")

    await _edit(client, token, item_id, text="Tom")

    session.expire_all()
    assert await _description(session, item_id) == "Tom\n\nA grey cat."


async def test_caption_edited_before_analysis_is_the_whole_description(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    item_id = await _image(client, token, text="old")

    await _edit(client, token, item_id, text="new")

    session.expire_all()
    assert await _description(session, item_id) == "new"


async def test_removing_caption_keeps_the_generated_description(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    item_id = await _image(client, token, text="our cat")
    await _set_description(session, item_id, "our cat\n\nA grey cat.")

    body = (await _edit(client, token, item_id, text="")).json()

    assert body["text"] is None
    session.expire_all()
    assert await session.get(TextContent, UUID(item_id)) is None
    assert await _description(session, item_id) == "A grey cat."


async def test_removing_the_only_searchable_text_removes_description_and_embedding(
    client: AsyncClient, session: AsyncSession, embedding_queue: FakeJobQueue
):
    _, token = await register_and_login(client)
    item_id = await _image(client, token, text="our cat")
    session.add(Embedding(item_id=UUID(item_id), embedding="[0]", content_hash="x"))
    await session.commit()
    embedding_queue.published.clear()

    assert (await _edit(client, token, item_id, text="  ")).status_code == 200

    session.expire_all()
    assert await _description(session, item_id) is None
    assert await session.get(Embedding, UUID(item_id)) is None
    assert embedding_queue.published == []


async def test_rename_file_changes_only_its_displayed_name(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)
    item_id = await _file(client, token)
    [storage_key] = storage.uploads

    body = (await _edit(client, token, item_id, filename="Q3 results.pdf")).json()

    assert body["file"]["filename"] == "Q3 results.pdf"
    # Downloads under the new name, from the same object.
    assert body["download_url"].startswith(f"https://fake-storage.test/{storage_key}?")
    assert "filename=Q3 results.pdf" in body["download_url"]
    session.expire_all()
    stored = await session.get(FileMetadata, UUID(item_id))
    assert (stored.filename, stored.storage_key) == ("Q3 results.pdf", storage_key)
    assert list(storage.uploads) == [storage_key]


async def test_rename_file_and_edit_caption_together(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    item_id = await _file(client, token, text="draft")

    body = (await _edit(client, token, item_id, filename="final.pdf", text="signed copy")).json()

    assert (body["file"]["filename"], body["text"]) == ("final.pdf", "signed copy")
    session.expire_all()
    assert await _description(session, item_id) == "signed copy"


async def test_filename_cannot_be_empty(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _file(client, token)

    response = await _edit(client, token, item_id, filename="  ")

    assert response.status_code == 422
    assert response.json()["detail"] == "Filename must not be empty"


async def test_only_files_have_a_filename(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "a note")

    response = await _edit(client, token, item_id, filename="note.txt")

    assert response.status_code == 422
    assert response.json()["detail"] == "Only files have a filename"


async def test_storage_details_are_not_editable(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    item_id = await _file(client, token)

    response = await _edit(client, token, item_id, storage_key="files/elsewhere.pdf")

    assert response.status_code == 422
    stored = await session.get(FileMetadata, UUID(item_id))
    assert stored.storage_key != "files/elsewhere.pdf"


async def test_cannot_edit_another_users_item(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    item_id = await _note(client, alice, "alice's")

    response = await _edit(client, bob, item_id, text="bob's now")

    assert response.status_code == 404
    [listed] = (await client.get("/items", headers=_auth(alice))).json()["items"]
    assert listed["text"] == "alice's"


async def test_edit_requires_auth(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "x")

    assert (await client.patch(f"/items/{item_id}", json={"text": "y"})).status_code == 401


async def test_add_caption_to_uncaptioned_file(
    client: AsyncClient, session: AsyncSession, embedding_queue: FakeJobQueue
):
    _, token = await register_and_login(client)
    item_id = await _file(client, token)
    await _set_description(session, item_id, "A quarterly sales report.")
    embedding_queue.published.clear()

    body = (await _edit(client, token, item_id, text="for the board meeting")).json()

    assert body["text"] == "for the board meeting"
    session.expire_all()
    assert await _description(session, item_id) == "for the board meeting\n\nA quarterly sales report."
    [job] = embedding_queue.published
    assert job.item_type == QueueItemType.file
