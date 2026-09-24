from httpx import AsyncClient

from conftest import FakeEmbedder
from helpers import register_and_login

# The vector similarity query itself is Postgres + pgvector only and these
# tests run on SQLite, so they cover the endpoint's contract up to the
# point where it would hit the index.


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


async def test_search_unavailable_when_query_cannot_be_embedded(client: AsyncClient, embedder: FakeEmbedder):
    embedder.fail = True
    _, token = await register_and_login(client)

    response = await client.post("/search", json={"query": "cat"}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 503
