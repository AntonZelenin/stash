from httpx import AsyncClient

from conftest import FakeObjectStorage
from helpers import register_and_login, upload_file, upload_image

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360000000020001e221bc330000000049454e"
    "44ae426082"
)
_EBML = bytes.fromhex("1a45dfa3") + bytes(16)
_ZIP = b"PK" + bytes.fromhex("0304") + bytes(64)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _note(client: AsyncClient, token: str, text: str) -> str:
    response = await client.post("/items/text", json={"text": text}, headers=_auth(token))
    assert response.status_code == 202
    return response.json()["id"]


async def _counts(client: AsyncClient, token: str) -> dict:
    response = await client.get("/items/counts", headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()


async def test_no_items_counts_zero_for_every_type(client: AsyncClient):
    _, token = await register_and_login(client)

    assert await _counts(client, token) == {
        "types": {"text": 0, "link": 0, "image": 0, "file": 0},
        "kinds": {"image": 0, "video": 0, "audio": 0, "document": 0, "book": 0, "other": 0},
        "favorites": 0,
    }


async def test_items_are_counted_by_type(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    await _note(client, token, "one")
    await _note(client, token, "two")
    await _note(client, token, "https://example.com")
    assert (await upload_image(client, storage, token, _PNG_BYTES)).status_code == 202
    assert (await upload_file(client, storage, token, "a.txt", b"hello")).status_code == 202
    assert (await upload_file(client, storage, token, "b.txt", b"hello")).status_code == 202

    counts = await _counts(client, token)

    assert counts["types"] == {"text": 2, "link": 1, "image": 1, "file": 2}


async def test_images_and_files_are_counted_by_kind(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    await _note(client, token, "notes and links have no kind")
    assert (await upload_image(client, storage, token, _PNG_BYTES)).status_code == 202
    files = {
        "clip.webm": _EBML,
        "clip.mkv": _EBML,
        "song.mp3": b"ID3" + bytes(16),
        "report.pdf": b"%PDF-1.7",
        "a.txt": b"hello",
        "b.txt": "привет".encode("cp1251"),  # stored without a charset
        "novel.epub": _ZIP,
        "backup.zip": _ZIP,
        "setup.exe": b"MZ",
    }
    for filename, data in files.items():
        assert (await upload_file(client, storage, token, filename, data)).status_code == 202

    counts = await _counts(client, token)

    assert counts["kinds"] == {"image": 1, "video": 2, "audio": 1, "document": 3, "book": 1, "other": 2}
    # Every file is still a file, whatever its kind.
    assert counts["types"]["file"] == len(files)


async def test_kind_counts_are_per_user(client: AsyncClient, storage: FakeObjectStorage):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    assert (await upload_file(client, storage, alice, "clip.webm", _EBML)).status_code == 202

    assert (await _counts(client, bob))["kinds"]["video"] == 0
    assert (await _counts(client, alice))["kinds"]["video"] == 1


async def test_favorites_are_counted(client: AsyncClient):
    _, token = await register_and_login(client)
    first = await _note(client, token, "one")
    await _note(client, token, "two")
    link = await _note(client, token, "https://example.com")
    for item_id in [first, link]:
        assert (await client.put(f"/items/{item_id}/favorite", headers=_auth(token))).status_code == 204

    assert (await _counts(client, token))["favorites"] == 2


async def test_favorites_count_covers_every_type_and_kind(client: AsyncClient, storage: FakeObjectStorage):
    """Favorites are counted once each, not once per type or kind group."""
    _, token = await register_and_login(client)
    note = await _note(client, token, "one")
    image = (await upload_image(client, storage, token, _PNG_BYTES)).json()["id"]
    book = (await upload_file(client, storage, token, "novel.epub", _ZIP)).json()["id"]
    await upload_file(client, storage, token, "other.epub", _ZIP)
    for item_id in [note, image, book]:
        assert (await client.put(f"/items/{item_id}/favorite", headers=_auth(token))).status_code == 204

    assert (await _counts(client, token))["favorites"] == 3


async def test_counts_follow_deletes_and_edits(client: AsyncClient):
    _, token = await register_and_login(client)
    note = await _note(client, token, "note")
    gone = await _note(client, token, "gone")

    assert (await client.delete(f"/items/{gone}", headers=_auth(token))).status_code == 204
    # A note edited into a URL becomes a link.
    response = await client.patch(f"/items/{note}", json={"text": "https://example.com"}, headers=_auth(token))
    assert response.status_code == 200

    assert (await _counts(client, token))["types"] == {"text": 0, "link": 1, "image": 0, "file": 0}


async def test_counts_are_per_user(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    await _note(client, alice, "alice's")

    assert (await _counts(client, bob))["types"]["text"] == 0
    assert (await _counts(client, alice))["types"]["text"] == 1


async def test_requires_token(client: AsyncClient):
    assert (await client.get("/items/counts")).status_code == 401
