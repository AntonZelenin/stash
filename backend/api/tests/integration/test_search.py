import pytest
from httpx import AsyncClient

from app.items.repos import ItemRepository
from conftest import FakeEmbedder, FakeQueryNormalizer
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


@pytest.fixture
def no_vector_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaces the pgvector query (Postgres only) with one matching
    nothing, so a search can complete on SQLite."""

    async def search_items(self, **kwargs):
        return []

    monkeypatch.setattr(ItemRepository, "search_items", search_items)


async def test_search_embeds_the_normalized_query(
    client: AsyncClient,
    embedder: FakeEmbedder,
    query_normalizer: FakeQueryNormalizer,
    no_vector_index: None,
):
    query_normalizer.rewrites["дівчина в помаранчевому светрі"] = "girl in an orange sweater"
    _, token = await register_and_login(client)

    response = await client.post(
        "/search", json={"query": "дівчина в помаранчевому светрі"}, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert query_normalizer.queries == ["дівчина в помаранчевому светрі"]
    assert embedder.queries == ["girl in an orange sweater"]


async def test_search_embeds_the_original_query_when_normalization_fails(
    client: AsyncClient,
    embedder: FakeEmbedder,
    query_normalizer: FakeQueryNormalizer,
    no_vector_index: None,
):
    query_normalizer.fail = True
    _, token = await register_and_login(client)

    response = await client.post("/search", json={"query": "книга про магію"}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert embedder.queries == ["книга про магію"]


async def test_search_unavailable_when_normalization_and_embedding_both_fail(
    client: AsyncClient, embedder: FakeEmbedder, query_normalizer: FakeQueryNormalizer
):
    query_normalizer.fail = True
    embedder.fail = True
    _, token = await register_and_login(client)

    response = await client.post("/search", json={"query": "cat"}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 503
