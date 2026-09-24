from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Description, ImageMetadata, Item, TextContent
from conftest import FakeObjectStorage
from helpers import register_and_login

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5a1e8b10000000049454e44ae426082"
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _note(client: AsyncClient, token: str, text: str) -> str:
    return (await client.post("/items/text", json={"text": text}, headers=_auth(token))).json()["id"]


async def _listed_ids(client: AsyncClient, token: str) -> set[str]:
    return {item["id"] for item in (await client.get("/items", headers=_auth(token))).json()["items"]}


async def test_delete_several_items(
    client: AsyncClient, session: AsyncSession, storage: FakeObjectStorage
):
    _, token = await register_and_login(client)
    note = await _note(client, token, "a note")
    link = await _note(client, token, "https://example.com")
    image = (
        await client.post(
            "/items/image",
            files={"file": ("photo.png", _PNG_BYTES, "image/png")},
            data={"text": "caption"},
            headers=_auth(token),
        )
    ).json()["id"]
    kept = await _note(client, token, "keep me")

    response = await client.post("/items/delete", json={"ids": [note, link, image]}, headers=_auth(token))

    assert response.status_code == 204
    assert await _listed_ids(client, token) == {kept}
    session.expire_all()
    for model in (Item, ImageMetadata, TextContent, Description):
        assert await session.get(model, UUID(image)) is None, model.__name__
    assert storage.uploads == {}


async def test_other_users_and_missing_items_are_skipped(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    alices = await _note(client, alice, "alice's")
    bobs = await _note(client, bob, "bob's")

    response = await client.post(
        "/items/delete", json={"ids": [alices, bobs, str(uuid4())]}, headers=_auth(bob)
    )

    assert response.status_code == 204
    assert await _listed_ids(client, alice) == {alices}
    assert await _listed_ids(client, bob) == set()


async def test_repeated_ids_are_fine(client: AsyncClient):
    _, token = await register_and_login(client)
    note = await _note(client, token, "x")

    response = await client.post("/items/delete", json={"ids": [note, note]}, headers=_auth(token))

    assert response.status_code == 204
    assert await _listed_ids(client, token) == set()


async def test_ids_are_required_and_limited(client: AsyncClient):
    _, token = await register_and_login(client)

    assert (await client.post("/items/delete", json={"ids": []}, headers=_auth(token))).status_code == 422
    too_many = [str(uuid4()) for _ in range(101)]
    assert (await client.post("/items/delete", json={"ids": too_many}, headers=_auth(token))).status_code == 422


async def test_delete_items_requires_auth(client: AsyncClient):
    assert (await client.post("/items/delete", json={"ids": [str(uuid4())]})).status_code == 401
