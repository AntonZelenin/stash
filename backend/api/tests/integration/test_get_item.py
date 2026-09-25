import uuid

from httpx import AsyncClient

from helpers import register_and_login


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _note(client: AsyncClient, token: str, text: str) -> str:
    return (await client.post("/items/text", json={"text": text}, headers=_auth(token))).json()["id"]


async def test_get_item(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "hello")

    response = await client.get(f"/items/{item_id}", headers=_auth(token))

    assert response.status_code == 200, response.text
    assert response.json()["id"] == item_id
    assert response.json()["text"] == "hello"


async def test_get_item_of_another_user_is_not_found(client: AsyncClient):
    _, owner = await register_and_login(client)
    item_id = await _note(client, owner, "private")
    _, other = await register_and_login(client, email="bob@example.com")

    assert (await client.get(f"/items/{item_id}", headers=_auth(other))).status_code == 404


async def test_get_missing_item_is_not_found(client: AsyncClient):
    _, token = await register_and_login(client)

    assert (await client.get(f"/items/{uuid.uuid4()}", headers=_auth(token))).status_code == 404


async def test_random_item_is_one_of_the_users_items(client: AsyncClient):
    _, token = await register_and_login(client)
    mine = {await _note(client, token, f"note {n}") for n in range(3)}
    _, other = await register_and_login(client, email="bob@example.com")
    await _note(client, other, "not mine")

    for _ in range(10):
        response = await client.get("/items/random", headers=_auth(token))
        assert response.status_code == 200, response.text
        assert response.json()["id"] in mine


async def test_random_item_without_items_is_not_found(client: AsyncClient):
    _, token = await register_and_login(client)

    assert (await client.get("/items/random", headers=_auth(token))).status_code == 404


async def test_random_item_requires_auth(client: AsyncClient):
    assert (await client.get("/items/random")).status_code == 401
