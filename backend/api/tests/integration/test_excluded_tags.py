"""`exclude_tag_id` / `excluded_tag_ids`: leaving out items carrying any of
the given tags (a client's hidden tags, in its Blind mode), on top of the
other filters, in listing, counts and "Surprise me" (search: see
`test_search.py`)."""

import uuid

from httpx import AsyncClient

from helpers import register_and_login


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _note(client: AsyncClient, token: str, text: str, tags: list[str] = ()) -> str:
    response = await client.post("/items/text", json={"text": text, "tags": list(tags)}, headers=_auth(token))
    assert response.status_code == 202, response.text
    return response.json()["id"]


async def _tag_ids(client: AsyncClient, token: str) -> dict[str, str]:
    response = await client.get("/tags", headers=_auth(token))
    return {tag["name"]: tag["id"] for tag in response.json()["tags"]}


async def _listed(client: AsyncClient, token: str, params: list[tuple[str, str]]) -> set[str]:
    response = await client.get("/items", params=params, headers=_auth(token))
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


async def test_items_carrying_any_excluded_tag_are_left_out(client: AsyncClient):
    _, token = await register_and_login(client)
    python = await _note(client, token, "python", ["python"])
    await _note(client, token, "archived python", ["python", "archive"])
    await _note(client, token, "nsfw", ["nsfw"])
    untagged = await _note(client, token, "no tags")
    tags = await _tag_ids(client, token)

    listed = await _listed(client, token, [("exclude_tag_id", tags["nsfw"]), ("exclude_tag_id", tags["archive"])])

    assert listed == {python, untagged}


async def test_no_excluded_tags_lists_everything(client: AsyncClient):
    _, token = await register_and_login(client)
    ids = {await _note(client, token, "a", ["nsfw"]), await _note(client, token, "b")}

    assert await _listed(client, token, []) == ids


async def test_exclusion_combines_with_the_tag_filter(client: AsyncClient):
    _, token = await register_and_login(client)
    kept = await _note(client, token, "kept", ["python"])
    await _note(client, token, "hidden", ["python", "archive"])
    tags = await _tag_ids(client, token)

    listed = await _listed(client, token, [("tag_id", tags["python"]), ("exclude_tag_id", tags["archive"])])

    assert listed == {kept}


async def test_excluding_an_unknown_or_other_users_tag_changes_nothing(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    await _note(client, alice, "a", ["secret"])
    alice_tag = (await _tag_ids(client, alice))["secret"]
    bobs = {await _note(client, bob, "b", ["secret"])}

    assert await _listed(client, bob, [("exclude_tag_id", alice_tag)]) == bobs
    assert await _listed(client, bob, [("exclude_tag_id", str(uuid.uuid4()))]) == bobs


async def test_counts_leave_out_excluded_items(client: AsyncClient):
    _, token = await register_and_login(client)
    await _note(client, token, "a note")
    await _note(client, token, "hidden note", ["nsfw"])
    await _note(client, token, "https://example.com", ["nsfw"])
    nsfw = (await _tag_ids(client, token))["nsfw"]

    everything = (await client.get("/items/counts", headers=_auth(token))).json()
    blind = (await client.get("/items/counts", params={"exclude_tag_id": nsfw}, headers=_auth(token))).json()

    assert everything["types"] == {"text": 2, "link": 1, "image": 0, "file": 0}
    assert blind["types"] == {"text": 1, "link": 0, "image": 0, "file": 0}


async def test_random_item_leaves_out_excluded_items(client: AsyncClient):
    _, token = await register_and_login(client)
    visible = await _note(client, token, "visible")
    await _note(client, token, "hidden", ["nsfw"])
    nsfw = (await _tag_ids(client, token))["nsfw"]

    for _ in range(10):
        response = await client.get("/items/random", params={"exclude_tag_id": nsfw}, headers=_auth(token))
        assert response.json()["id"] == visible


async def test_random_item_is_not_found_when_everything_is_excluded(client: AsyncClient):
    _, token = await register_and_login(client)
    await _note(client, token, "hidden", ["nsfw"])
    nsfw = (await _tag_ids(client, token))["nsfw"]

    response = await client.get("/items/random", params={"exclude_tag_id": nsfw}, headers=_auth(token))

    assert response.status_code == 404


async def test_too_many_or_invalid_excluded_tags_are_rejected(client: AsyncClient):
    _, token = await register_and_login(client)
    too_many = [("exclude_tag_id", str(uuid.uuid4())) for _ in range(101)]

    assert (await client.get("/items", params=too_many, headers=_auth(token))).status_code == 422
    invalid = {"exclude_tag_id": "not-a-uuid"}
    for path in ("/items", "/items/counts", "/items/random"):
        assert (await client.get(path, params=invalid, headers=_auth(token))).status_code == 422, path
    response = await client.post(
        "/search",
        json={"query": "x", "excluded_tag_ids": [str(uuid.uuid4()) for _ in range(101)]},
        headers=_auth(token),
    )
    assert response.status_code == 422
