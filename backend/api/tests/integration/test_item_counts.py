from httpx import AsyncClient

from conftest import FakeObjectStorage
from helpers import register_and_login, upload_file, upload_image

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360000000020001e221bc330000000049454e"
    "44ae426082"
)


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


async def test_favorites_are_counted(client: AsyncClient):
    _, token = await register_and_login(client)
    first = await _note(client, token, "one")
    await _note(client, token, "two")
    link = await _note(client, token, "https://example.com")
    for item_id in [first, link]:
        assert (await client.put(f"/items/{item_id}/favorite", headers=_auth(token))).status_code == 204

    assert (await _counts(client, token))["favorites"] == 2


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
