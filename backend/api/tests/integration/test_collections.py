import pytest
from httpx import AsyncClient

from conftest import FakeObjectStorage
from helpers import register_and_login, upload_file, upload_image

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5a1e8b10000000049454e44ae426082"
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _note(client: AsyncClient, token: str, text: str, **fields) -> str:
    response = await client.post("/items/text", json={"text": text, **fields}, headers=_auth(token))
    assert response.status_code == 202, response.text
    return response.json()["id"]


async def _add(client: AsyncClient, token: str, item_id: str, name: str):
    return await client.post(f"/items/{item_id}/collections", json={"name": name}, headers=_auth(token))


async def _remove(client: AsyncClient, token: str, item_id: str, collection_id: str):
    return await client.delete(f"/items/{item_id}/collections/{collection_id}", headers=_auth(token))


async def _listed(client: AsyncClient, token: str, params=None) -> list[dict]:
    response = await client.get("/items", params=params, headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _collections(client: AsyncClient, token: str, **params) -> list[dict]:
    response = await client.get("/collections", params=params, headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()["collections"]


def _names(item: dict) -> list[str]:
    return [collection["name"] for collection in item["collections"]]


# ---- adding and removing ----


async def test_adding_creates_the_collection_and_shows_it_on_the_item(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "pasta recipe")

    response = await _add(client, token, item_id, "Recipes")

    assert response.status_code == 200
    collection = response.json()
    assert collection["name"] == "Recipes"
    [item] = await _listed(client, token)
    assert item["collections"] == [collection]
    assert item["tags"] == []


async def test_existing_collection_is_reused_ignoring_case(client: AsyncClient):
    _, token = await register_and_login(client)
    first = await _note(client, token, "one")
    second = await _note(client, token, "two")

    recipes = (await _add(client, token, first, "Recipes")).json()
    again = (await _add(client, token, second, "  recipes ")).json()

    assert again == recipes
    assert await _collections(client, token) == [recipes]


async def test_adding_twice_is_a_no_op(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")

    await _add(client, token, item_id, "Trips")
    response = await _add(client, token, item_id, "trips")

    assert response.status_code == 200
    [item] = await _listed(client, token)
    assert _names(item) == ["Trips"]


async def test_an_item_can_be_in_many_collections_sorted_by_name(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")
    for name in ["Trips", "Books", "Recipes"]:
        await _add(client, token, item_id, name)

    [item] = await _listed(client, token)

    assert _names(item) == ["Books", "Recipes", "Trips"]


async def test_collections_and_tags_are_separate(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")

    await _add(client, token, item_id, "Python")
    await client.post(f"/items/{item_id}/tags", json={"name": "Python"}, headers=_auth(token))

    [item] = await _listed(client, token)
    assert _names(item) == ["Python"]
    assert [tag["name"] for tag in item["tags"]] == ["Python"]
    assert item["collections"][0]["id"] != item["tags"][0]["id"]


@pytest.mark.parametrize("name", ["", "   ", "x" * 51])
async def test_invalid_collection_names_are_rejected(client: AsyncClient, name: str):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")

    response = await _add(client, token, item_id, name)

    assert response.status_code == 422
    assert await _collections(client, token) == []


async def test_removing_the_last_item_deletes_the_collection(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")
    collection = (await _add(client, token, item_id, "Trips")).json()

    response = await _remove(client, token, item_id, collection["id"])

    assert response.status_code == 204
    [item] = await _listed(client, token)
    assert item["collections"] == []
    assert await _collections(client, token) == []


async def test_removing_an_item_from_a_collection_others_are_in_keeps_it(client: AsyncClient):
    _, token = await register_and_login(client)
    first = await _note(client, token, "one", collections=["Trips"])
    await _note(client, token, "two", collections=["Trips"])
    [collection] = await _collections(client, token)

    await _remove(client, token, first, collection["id"])

    assert await _collections(client, token) == [collection]


async def test_an_emptied_collection_name_can_be_used_again(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")
    old = (await _add(client, token, item_id, "Trips")).json()
    await _remove(client, token, item_id, old["id"])

    new = (await _add(client, token, item_id, "Trips")).json()

    assert new["id"] != old["id"]
    assert await _collections(client, token) == [new]


async def test_removing_from_a_collection_the_item_is_not_in_is_a_no_op(client: AsyncClient):
    _, token = await register_and_login(client)
    first = await _note(client, token, "one")
    second = await _note(client, token, "two")
    collection = (await _add(client, token, first, "Trips")).json()

    response = await _remove(client, token, second, collection["id"])

    assert response.status_code == 204
    listed = {item["id"]: _names(item) for item in await _listed(client, token)}
    assert listed == {first: ["Trips"], second: []}


async def test_deleting_an_items_last_item_deletes_the_collection(client: AsyncClient):
    _, token = await register_and_login(client)
    only = await _note(client, token, "only", collections=["Trips"])
    shared = await _note(client, token, "shared", collections=["Trips", "Books"])
    await _note(client, token, "other", collections=["Books"])

    await client.delete(f"/items/{only}", headers=_auth(token))
    assert [c["name"] for c in await _collections(client, token)] == ["Books", "Trips"]

    await client.delete(f"/items/{shared}", headers=_auth(token))
    assert [c["name"] for c in await _collections(client, token)] == ["Books"]


async def test_deleting_every_item_of_a_collection_at_once_deletes_it(
    client: AsyncClient, storage: FakeObjectStorage
):
    """The reported case: all the images of a collection deleted."""
    _, token = await register_and_login(client)
    images = [
        (await upload_image(client, storage, token, _PNG_BYTES, collections=["Photos"])).json()["id"]
        for _ in range(2)
    ]
    kept = await _note(client, token, "kept", collections=["Trips"])

    response = await client.post("/items/delete", json={"ids": images}, headers=_auth(token))

    assert response.status_code == 204
    assert [c["name"] for c in await _collections(client, token)] == ["Trips"]
    [item] = await _listed(client, token)
    assert item["id"] == kept


async def test_requires_token(client: AsyncClient):
    assert (await client.get("/collections")).status_code == 401
    assert (await client.post("/items/00000000-0000-0000-0000-000000000000/collections", json={"name": "x"})).status_code == 401


async def test_users_have_separate_collections(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    alices = (await _add(client, alice, await _note(client, alice, "a"), "Trips")).json()
    bobs = (await _add(client, bob, await _note(client, bob, "b"), "Trips")).json()

    assert alices["id"] != bobs["id"]
    assert await _collections(client, alice) == [alices]
    assert await _collections(client, bob) == [bobs]


async def test_cannot_change_another_users_item(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    item_id = await _note(client, alice, "a")
    collection = (await _add(client, alice, item_id, "Trips")).json()

    assert (await _add(client, bob, item_id, "Stolen")).status_code == 404
    assert (await _remove(client, bob, item_id, collection["id"])).status_code == 404
    [item] = await _listed(client, alice)
    assert _names(item) == ["Trips"]
    assert await _collections(client, bob) == []


async def test_collection_search_matches_anywhere_prefix_first(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")
    for name in ["Summer trips", "Trips", "Books"]:
        await _add(client, token, item_id, name)

    found = await _collections(client, token, query="TRIP")

    assert [collection["name"] for collection in found] == ["Trips", "Summer trips"]


async def test_collection_search_treats_wildcards_literally(client: AsyncClient):
    _, token = await register_and_login(client)
    item_id = await _note(client, token, "one")
    for name in ["100% done", "1000 done"]:
        await _add(client, token, item_id, name)

    found = await _collections(client, token, query="0%")

    assert [collection["name"] for collection in found] == ["100% done"]


# ---- saving items into collections ----


async def test_text_item_created_in_collections(client: AsyncClient):
    _, token = await register_and_login(client)

    await _note(client, token, "one", collections=["Trips", " Books ", "TRIPS"], tags=["Python"])

    [item] = await _listed(client, token)
    # Duplicates dropped, first spelling wins; tags unaffected.
    assert _names(item) == ["Books", "Trips"]
    assert [tag["name"] for tag in item["tags"]] == ["Python"]


async def test_uploads_in_one_batch_share_their_collections(client: AsyncClient, storage: FakeObjectStorage):
    _, token = await register_and_login(client)
    trips = (await _add(client, token, await _note(client, token, "one"), "Trips")).json()

    image = await upload_image(client, storage, token, _PNG_BYTES, collections=["trips", "Photos"])
    document = await upload_file(client, storage, token, "plan.txt", b"day one", collections=["trips", "Photos"])

    assert (image.status_code, document.status_code) == (202, 202)
    listed = {item["id"]: item for item in await _listed(client, token)}
    for created in (image, document):
        item = listed[created.json()["id"]]
        assert _names(item) == ["Photos", "Trips"]
        assert trips in item["collections"]
    assert len(await _collections(client, token)) == 2


async def test_invalid_collections_reject_the_item_before_anything_is_stored(client: AsyncClient):
    _, token = await register_and_login(client)

    too_many = [f"c{n}" for n in range(21)]
    responses = [
        await client.post("/items/text", json={"text": "x", "collections": [" "]}, headers=_auth(token)),
        await client.post("/items/text", json={"text": "x", "collections": too_many}, headers=_auth(token)),
        await client.post(
            "/uploads",
            json={"type": "file", "filename": "a.txt", "size_bytes": 1, "collections": ["x" * 51]},
            headers=_auth(token),
        ),
    ]

    assert [response.status_code for response in responses] == [422, 422, 422]
    assert await _listed(client, token) == []
    assert await _collections(client, token) == []


# ---- filtering items ----


async def test_filter_by_one_collection(client: AsyncClient):
    _, token = await register_and_login(client)
    inside = await _note(client, token, "inside", collections=["Trips"])
    await _note(client, token, "outside")
    [collection] = await _collections(client, token)

    listed = await _listed(client, token, {"collection_id": collection["id"]})

    assert [item["id"] for item in listed] == [inside]


async def test_multiple_collections_match_items_in_any_of_them(client: AsyncClient):
    _, token = await register_and_login(client)
    trip = await _note(client, token, "trip", collections=["Trips"])
    book = await _note(client, token, "book", collections=["Books"])
    both = await _note(client, token, "both", collections=["Trips", "Books"])
    await _note(client, token, "neither", collections=["Recipes"])
    ids = {collection["name"]: collection["id"] for collection in await _collections(client, token)}

    listed = await _listed(client, token, [("collection_id", ids["Trips"]), ("collection_id", ids["Books"])])

    # Each item once, even if it's in both.
    assert sorted(item["id"] for item in listed) == sorted([trip, book, both])


async def test_collections_and_tags_combine(client: AsyncClient):
    _, token = await register_and_login(client)
    wanted = await _note(client, token, "wanted", collections=["Trips"], tags=["Read later"])
    await _note(client, token, "untagged", collections=["Trips"])
    await _note(client, token, "elsewhere", tags=["Read later"])
    [collection] = await _collections(client, token)
    [tag] = (await client.get("/tags", headers=_auth(token))).json()["tags"]

    listed = await _listed(client, token, {"collection_id": collection["id"], "tag_id": tag["id"]})

    assert [item["id"] for item in listed] == [wanted]


async def test_collection_filter_combines_with_type_and_sorting(client: AsyncClient):
    _, token = await register_and_login(client)
    await _note(client, token, "a note", collections=["Trips"])
    link = await _note(client, token, "https://example.com", collections=["Trips"])
    [collection] = await _collections(client, token)

    by_type = await _listed(client, token, {"collection_id": collection["id"], "type": "link"})
    shuffled = await _listed(client, token, {"collection_id": collection["id"], "sort": "random"})

    assert [item["id"] for item in by_type] == [link]
    assert len(shuffled) == 2


async def test_filtering_by_another_users_collection_finds_nothing(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")
    await _note(client, alice, "a", collections=["Secret"])
    await _note(client, bob, "b")
    [alices] = await _collections(client, alice)

    assert await _listed(client, bob, {"collection_id": alices["id"]}) == []


async def test_invalid_collection_filter_is_rejected(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.get("/items", params={"collection_id": "not-a-uuid"}, headers=_auth(token))

    assert response.status_code == 422

