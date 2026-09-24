import uuid

import pytest
from httpx import AsyncClient

from helpers import register_and_login


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _note(client: AsyncClient, token: str, text: str) -> str:
    response = await client.post("/items/text", json={"text": text}, headers=_auth(token))
    return response.json()["id"]


async def _tag(client: AsyncClient, token: str, item_id: str, name: str):
    return await client.post(f"/items/{item_id}/tags", json={"name": name}, headers=_auth(token))


async def _listed(client: AsyncClient, token: str, **params) -> list[dict]:
    response = await client.get("/items", params=params, headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()["items"]


# ---- assigning and removing ----


async def test_assign_creates_tag_and_shows_it_on_the_item(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "asyncio notes")

    response = await _tag(client, token, item_id, "Python")

    assert response.status_code == 200
    tag = response.json()
    assert tag["name"] == "Python"
    [item] = await _listed(client, token)
    assert item["tags"] == [tag]


async def test_existing_tag_is_reused_ignoring_case(client: AsyncClient):
    _, token = await register_and_login(client)
    first = await _note(client, token, "one")
    second = await _note(client, token, "two")

    python = (await _tag(client, token, first, "Python")).json()
    again = (await _tag(client, token, second, "  python ")).json()

    # Same tag, keeping the name as first entered.
    assert again == python
    tags = (await client.get("/tags", headers=_auth(token))).json()["tags"]
    assert tags == [python]


async def test_assigning_twice_is_a_no_op(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")

    await _tag(client, token, item_id, "AWS")
    response = await _tag(client, token, item_id, "aws")

    assert response.status_code == 200
    [item] = await _listed(client, token)
    assert [t["name"] for t in item["tags"]] == ["AWS"]


async def test_tags_on_an_item_are_sorted_by_name(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")
    for name in ["python", "architecture", "books"]:
        await _tag(client, token, item_id, name)

    [item] = await _listed(client, token)

    assert [t["name"] for t in item["tags"]] == ["architecture", "books", "python"]


async def test_tag_name_whitespace_is_normalized(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")

    tag = (await _tag(client, token, item_id, "  machine   learning ")).json()

    assert tag["name"] == "machine learning"


@pytest.mark.parametrize("name", ["", "   ", "x" * 51])
async def test_invalid_tag_names_are_rejected(client: AsyncClient, name):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")

    response = await _tag(client, token, item_id, name)

    assert response.status_code == 422


async def test_remove_tag_from_item_keeps_the_tag(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")
    tag = (await _tag(client, token, item_id, "Books")).json()

    response = await client.delete(f"/items/{item_id}/tags/{tag['id']}", headers=_auth(token))

    assert response.status_code == 204
    [item] = await _listed(client, token)
    assert item["tags"] == []
    # Still in the user's tag list, ready to reuse.
    assert (await client.get("/tags", headers=_auth(token))).json()["tags"] == [tag]


async def test_deleting_an_item_removes_its_tag_links(client: AsyncClient):
    _, token = await register_and_login(client)
    doomed = await _note(client, token, "doomed")
    kept = await _note(client, token, "kept")
    tag = (await _tag(client, token, doomed, "Temp")).json()
    await _tag(client, token, kept, "Temp")

    await client.delete(f"/items/{doomed}", headers=_auth(token))

    listed = await _listed(client, token, tag_id=tag["id"])
    assert [item["id"] for item in listed] == [kept]


async def test_requires_token(client: AsyncClient):
    item_id = uuid.uuid4()
    assert (await client.get("/tags")).status_code == 401
    assert (await client.post(f"/items/{item_id}/tags", json={"name": "x"})).status_code == 401
    assert (await client.delete(f"/items/{item_id}/tags/{uuid.uuid4()}")).status_code == 401


# ---- per-user isolation ----


async def test_users_have_separate_tags(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    alice_tag = (await _tag(client, alice, await _note(client, alice, "a"), "Python")).json()
    bob_tag = (await _tag(client, bob, await _note(client, bob, "b"), "Python")).json()

    assert alice_tag["id"] != bob_tag["id"]
    assert (await client.get("/tags", headers=_auth(alice))).json()["tags"] == [alice_tag]
    assert (await client.get("/tags", headers=_auth(bob))).json()["tags"] == [bob_tag]


async def test_cannot_tag_or_untag_another_users_item(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    item_id = await _note(client, alice, "alice's note")
    tag = (await _tag(client, alice, item_id, "Private")).json()

    assert (await _tag(client, bob, item_id, "Mine")).status_code == 404
    assert (await client.delete(f"/items/{item_id}/tags/{tag['id']}", headers=_auth(bob))).status_code == 404
    [item] = await _listed(client, alice)
    assert item["tags"] == [tag]


# ---- searching tags ----


async def test_tag_search_matches_anywhere_prefix_first(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")
    for name in ["Books", "Python", "Cython", "AWS", "python-tips"]:
        await _tag(client, token, item_id, name)

    response = await client.get("/tags", params={"query": "YTH"}, headers=_auth(token))

    assert [t["name"] for t in response.json()["tags"]] == ["Cython", "Python", "python-tips"]
    response = await client.get("/tags", params={"query": "py"}, headers=_auth(token))
    assert [t["name"] for t in response.json()["tags"]] == ["Python", "python-tips"]


async def test_tag_search_treats_wildcards_literally(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")
    for name in ["100% done", "plain", "snake_case"]:
        await _tag(client, token, item_id, name)

    async def names(query: str) -> list[str]:
        response = await client.get("/tags", params={"query": query}, headers=_auth(token))
        return [t["name"] for t in response.json()["tags"]]

    assert await names("%") == ["100% done"]
    assert await names("_") == ["snake_case"]


# ---- filtering items ----


async def test_filter_by_one_tag(client: AsyncClient):
    _, token = await register_and_login(client)
    tagged = await _note(client, token, "tagged")
    await _note(client, token, "untagged")
    tag = (await _tag(client, token, tagged, "Python")).json()

    listed = await _listed(client, token, tag_id=tag["id"])

    assert [item["id"] for item in listed] == [tagged]


async def test_multiple_tags_narrow_results(client: AsyncClient):
    """An item must carry every selected tag."""
    _, token = await register_and_login(client)
    both = await _note(client, token, "both")
    only_python = await _note(client, token, "only python")
    python = (await _tag(client, token, both, "Python")).json()
    aws = (await _tag(client, token, both, "AWS")).json()
    await _tag(client, token, only_python, "Python")

    response = await client.get(
        "/items", params=[("tag_id", python["id"]), ("tag_id", aws["id"])], headers=_auth(token)
    )

    assert [item["id"] for item in response.json()["items"]] == [both]


async def test_filter_by_type(client: AsyncClient):
    _, token = await register_and_login(client)
    note = await _note(client, token, "a note")
    link = await _note(client, token, "https://example.com")

    assert [i["id"] for i in await _listed(client, token, type="text")] == [note]
    assert [i["id"] for i in await _listed(client, token, type="link")] == [link]
    assert await _listed(client, token, type="image") == []


async def test_type_and_tags_combine(client: AsyncClient):
    _, token = await register_and_login(client)
    note = await _note(client, token, "a note")
    link = await _note(client, token, "https://example.com")
    tag = (await _tag(client, token, note, "Read later")).json()
    await _tag(client, token, link, "Read later")

    listed = await _listed(client, token, type="link", tag_id=tag["id"])

    assert [item["id"] for item in listed] == [link]


async def test_filtering_by_another_users_tag_finds_nothing(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    alice_tag = (await _tag(client, alice, await _note(client, alice, "a"), "Secret")).json()
    await _note(client, bob, "b")

    assert await _listed(client, bob, tag_id=alice_tag["id"]) == []


async def test_invalid_filters_are_rejected(client: AsyncClient):
    _, token = await register_and_login(client)

    assert (await client.get("/items", params={"type": "video"}, headers=_auth(token))).status_code == 422
    assert (await client.get("/items", params={"tag_id": "not-a-uuid"}, headers=_auth(token))).status_code == 422
