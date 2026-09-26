"""`GET /items?kind=...`: the Media (image, video, audio) and Files
(document, book, other) filters, one kind or a whole group. A file's kind follows from its stored
content type (see `app.items.files`)."""

from httpx import AsyncClient

from conftest import FakeObjectStorage
from helpers import register_and_login, upload_file, upload_image

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360000000020001e221bc330000000049454e"
    "44ae426082"
)
_ZIP = b"PK\x03\x04" + b"\x00" * 64

# One of each kind of file, by filename.
_FILES = {
    "clip.mp4": b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32,
    "song.mp3": b"ID3\x04\x00" + b"\x00" * 32,
    "report.pdf": b"%PDF-1.7\n%%EOF\n",
    "notes.txt": b"buy milk",
    "novel.epub": _ZIP,
    "tale.fb2": b'<?xml version="1.0"?><FictionBook><body/></FictionBook>',
    "backup.zip": _ZIP,
    "setup.exe": b"MZ\x90\x00",
    # Content doesn't match the extension: a generic file, so `other`.
    "fake.mp4": b"MZ\x90\x00",
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _name(item: dict) -> str:
    """A file's filename, "image", or a note's/link's text."""
    if item["file"]:
        return item["file"]["filename"]
    return "image" if item["type"] == "image" else item["text"]


async def _listed_names(client: AsyncClient, token: str, **params) -> set[str]:
    response = await client.get("/items", params=params, headers=_auth(token))
    assert response.status_code == 200, response.text
    return {_name(item) for item in response.json()["items"]}


async def _stash(client: AsyncClient, storage: FakeObjectStorage, token: str) -> dict[str, str]:
    """Saves a note, a link, an image and every file in `_FILES`; returns
    their ids by name ("note", "link", "image", or the filename)."""
    ids = {}
    for name, text in (("note", "a note"), ("link", "https://example.com")):
        response = await client.post("/items/text", json={"text": text}, headers=_auth(token))
        ids[name] = response.json()["id"]
    ids["image"] = (await upload_image(client, storage, token, _PNG_BYTES)).json()["id"]
    for filename, data in _FILES.items():
        response = await upload_file(client, storage, token, filename, data)
        assert response.status_code == 202, response.text
        ids[filename] = response.json()["id"]
    return ids


async def test_media_kinds(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    await _stash(client, storage, token)

    assert await _listed_names(client, token, kind="image") == {"image"}
    assert await _listed_names(client, token, kind="video") == {"clip.mp4"}
    assert await _listed_names(client, token, kind="audio") == {"song.mp3"}


async def test_file_kinds(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    await _stash(client, storage, token)

    # PDFs are documents: nothing reliable says a PDF is a book.
    assert await _listed_names(client, token, kind="document") == {"report.pdf", "notes.txt"}
    assert await _listed_names(client, token, kind="book") == {"novel.epub", "tale.fb2"}
    # Archives and generic files, but never media, documents or books.
    assert await _listed_names(client, token, kind="other") == {"backup.zip", "setup.exe", "fake.mp4"}


async def test_several_kinds_match_any_of_them(client: AsyncClient, storage: FakeObjectStorage):
    """"All Media" and "All Files" send each kind of their group."""
    _, token = await register_and_login(client)
    await _stash(client, storage, token)

    media = await _listed_names(client, token, kind=["image", "video", "audio"])
    files = await _listed_names(client, token, kind=["document", "book", "other"])

    assert media == {"image", "clip.mp4", "song.mp3"}
    assert files == {"report.pdf", "notes.txt", "novel.epub", "tale.fb2", "backup.zip", "setup.exe", "fake.mp4"}
    # Mixed: images and files together, `other` among other file kinds.
    assert await _listed_names(client, token, kind=["image", "other"]) == {"image", "backup.zip", "setup.exe", "fake.mp4"}
    assert await _listed_names(client, token, kind=["book", "book"]) == {"novel.epub", "tale.fb2"}


async def test_several_kinds_combine_with_favorites(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    ids = await _stash(client, storage, token)
    for name in ("clip.mp4", "report.pdf"):
        assert (await client.put(f"/items/{ids[name]}/favorite", headers=_auth(token))).status_code == 204

    media = await _listed_names(client, token, kind=["image", "video", "audio"], favorite="true")

    assert media == {"clip.mp4"}


async def test_no_kind_lists_everything(client: AsyncClient, storage: FakeObjectStorage):
    """What "All Items" sends: neither a type nor a kind."""
    _, token = await register_and_login(client)
    ids = await _stash(client, storage, token)

    response = await client.get("/items", headers=_auth(token))

    assert {item["id"] for item in response.json()["items"]} == set(ids.values())


async def test_kind_combines_with_favorites(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    ids = await _stash(client, storage, token)
    for name in ("novel.epub", "note"):
        assert (await client.put(f"/items/{ids[name]}/favorite", headers=_auth(token))).status_code == 204

    assert await _listed_names(client, token, kind="book", favorite="true") == {"novel.epub"}
    assert await _listed_names(client, token, kind="book") == {"novel.epub", "tale.fb2"}
    # Favorites alone still covers every type.
    assert await _listed_names(client, token, favorite="true") == {"novel.epub", "a note"}


async def test_kind_and_type_must_both_match(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    await _stash(client, storage, token)

    assert await _listed_names(client, token, type="file", kind="video") == {"clip.mp4"}
    assert await _listed_names(client, token, type="image", kind="video") == set()
    assert await _listed_names(client, token, type="text", kind="other") == set()


async def test_kind_filter_only_covers_the_users_own_items(client: AsyncClient, storage: FakeObjectStorage):
    _, other = await register_and_login(client, email="mallory@example.com")
    await _stash(client, storage, other)
    _, token = await register_and_login(client)

    assert await _listed_names(client, token, kind="video") == set()
    assert await _listed_names(client, token, kind="other") == set()


async def test_listed_files_carry_their_kind(client: AsyncClient, storage: FakeObjectStorage):
    """So clients can tell media files apart without their own MIME table."""
    _, token = await register_and_login(client)
    await _stash(client, storage, token)

    response = await client.get("/items", headers=_auth(token))

    kinds = {item["file"]["filename"]: item["file"]["kind"] for item in response.json()["items"] if item["file"]}
    assert kinds == {
        "clip.mp4": "video",
        "song.mp3": "audio",
        "report.pdf": "document",
        "notes.txt": "document",
        "novel.epub": "book",
        "tale.fb2": "book",
        "backup.zip": "other",
        "setup.exe": "other",
        "fake.mp4": "other",
    }


async def test_rejects_unknown_kind(client: AsyncClient):
    _, token = await register_and_login(client)

    for kind in ("file", "media", "pdf"):
        response = await client.get("/items", params={"kind": kind}, headers=_auth(token))
        assert response.status_code == 422, kind
