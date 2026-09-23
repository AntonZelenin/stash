from httpx import AsyncClient

from helpers import register_and_login

# The full-text match itself (tsvector/tsquery) is Postgres-only and these
# tests run on SQLite, so they cover the endpoint's contract up to the
# point where it would hit the search index.


async def test_search_requires_token(client: AsyncClient):
    response = await client.post("/search", json={"query": "cat"})

    assert response.status_code == 401


async def test_search_rejects_empty_query(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.post("/search", json={"query": ""}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 422


async def test_search_rejects_out_of_range_limit(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.post(
        "/search", json={"query": "cat", "limit": 101}, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 422


async def test_search_with_no_searchable_words_returns_nothing(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.post("/search", json={"query": "?!&"}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"items": []}
