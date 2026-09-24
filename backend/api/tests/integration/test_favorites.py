import uuid

from httpx import AsyncClient

from helpers import register_and_login


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _note(client: AsyncClient, token: str, text: str) -> str:
    return (await client.post("/items/text", json={"text": text}, headers=_auth(token))).json()["id"]


async def _listed(client: AsyncClient, token: str, **params) -> list[dict]:
    response = await client.get("/items", params=params, headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def test_items_start_out_not_favorite(client: AsyncClient):
    _, token = await register_and_login(client)
    await _note(client, token, "plain")

    [item] = await _listed(client, token)

    assert item["is_favorite"] is False


async def test_mark_and_unmark_favorite(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "keep this")

    marked = await client.put(f"/items/{item_id}/favorite", headers=_auth(token))
    assert marked.status_code == 204
    assert (await _listed(client, token))[0]["is_favorite"] is True

    unmarked = await client.delete(f"/items/{item_id}/favorite", headers=_auth(token))
    assert unmarked.status_code == 204
    assert (await _listed(client, token))[0]["is_favorite"] is False


async def test_marking_is_idempotent(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "keep this")

    for _ in range(2):
        assert (await client.put(f"/items/{item_id}/favorite", headers=_auth(token))).status_code == 204
    for _ in range(2):
        assert (await client.delete(f"/items/{item_id}/favorite", headers=_auth(token))).status_code == 204


async def test_filter_favorites_only(client: AsyncClient):
    _, token = await register_and_login(client)
    favorite = await _note(client, token, "favorite")
    await _note(client, token, "ordinary")
    await client.put(f"/items/{favorite}/favorite", headers=_auth(token))

    assert [i["id"] for i in await _listed(client, token, favorite="true")] == [favorite]
    assert len(await _listed(client, token, favorite="false")) == 2
    assert len(await _listed(client, token)) == 2


async def test_favorites_combine_with_type_and_tags(client: AsyncClient):
    _, token = await register_and_login(client)
    fav_note = await _note(client, token, "a note")
    fav_link = await _note(client, token, "https://example.com")
    plain_link = await _note(client, token, "https://example.org")
    for item_id in (fav_note, fav_link):
        await client.put(f"/items/{item_id}/favorite", headers=_auth(token))
    tag = (await client.post(f"/items/{fav_link}/tags", json={"name": "Read"}, headers=_auth(token))).json()
    await client.post(f"/items/{plain_link}/tags", json={"name": "Read"}, headers=_auth(token))

    assert [i["id"] for i in await _listed(client, token, favorite="true", type="link")] == [fav_link]
    assert [i["id"] for i in await _listed(client, token, favorite="true", tag_id=tag["id"])] == [fav_link]


async def test_cannot_favorite_another_users_item(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    item_id = await _note(client, alice, "alice's")

    assert (await client.put(f"/items/{item_id}/favorite", headers=_auth(bob))).status_code == 404
    assert (await client.delete(f"/items/{item_id}/favorite", headers=_auth(bob))).status_code == 404
    assert (await _listed(client, alice))[0]["is_favorite"] is False


async def test_missing_item_and_auth(client: AsyncClient):
    _, token = await register_and_login(client)
    missing = uuid.uuid4()

    assert (await client.put(f"/items/{missing}/favorite", headers=_auth(token))).status_code == 404
    assert (await client.put(f"/items/{missing}/favorite")).status_code == 401
    assert (await client.delete(f"/items/{missing}/favorite")).status_code == 401
