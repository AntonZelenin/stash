import pytest
from httpx import AsyncClient
from stash_shared import secrets

from app import embeddings
from app.config import get_settings
from app.embeddings import get_embedder
from app.items.repos import ItemRepository
from app.main import app
from app.query_normalization import OpenAIQueryNormalizer, get_query_normalizer
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


class _FakeOpenAIEmbedder(FakeEmbedder):
    """Stands in for `stash_shared.embeddings.OpenAIEmbedder`, recording the
    key it was created with."""

    created_with: list[str] = []

    def __init__(self, *, api_key: str, model: str):
        super().__init__()
        self.created_with.append(api_key)


@pytest.fixture
def unfilled_openai_secret(monkeypatch: pytest.MonkeyPatch):
    """The real search dependencies (no fakes), on AWS settings whose
    OpenAI secret exists but has no value yet, like right after Terraform
    created it. Yields the secret values; set the key to fill it."""
    arn = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:stash-prod/openai/api-key-AbCdEf"
    values: dict[str, str] = {}
    fetched: list[str] = []

    def get_secret_string(secret_arn: str) -> str:
        fetched.append(secret_arn)
        if secret_arn not in values:
            raise LookupError("Secrets Manager can't find the specified secret value for staging label: AWSCURRENT")
        return values[secret_arn]

    monkeypatch.setattr(secrets, "get_secret_string", get_secret_string)
    monkeypatch.setattr(get_settings(), "openai_api_key_secret_arn", arn)
    monkeypatch.setattr(embeddings, "OpenAIEmbedder", _FakeOpenAIEmbedder)
    _FakeOpenAIEmbedder.created_with = []
    app.dependency_overrides.pop(get_embedder, None)
    app.dependency_overrides.pop(get_query_normalizer, None)
    get_embedder.cache_clear()
    get_query_normalizer.cache_clear()
    yield {"arn": arn, "values": values, "fetched": fetched}
    get_embedder.cache_clear()
    get_query_normalizer.cache_clear()


async def test_without_the_openai_key_only_search_is_unavailable(
    client: AsyncClient, unfilled_openai_secret, no_vector_index: None
):
    health = await client.get("/health")
    _, token = await register_and_login(client)  # registration and login
    items = await client.get("/items", headers={"Authorization": f"Bearer {token}"})

    search = await client.post("/search", json={"query": "cat"}, headers={"Authorization": f"Bearer {token}"})

    assert health.status_code == 200
    assert token
    assert items.status_code == 200
    assert search.status_code == 503


async def test_search_works_once_the_openai_key_is_set_and_fetches_it_once(
    client: AsyncClient, unfilled_openai_secret, no_vector_index: None, monkeypatch: pytest.MonkeyPatch
):
    # Normalization is best-effort; keep it out of the way (it would call OpenAI).
    monkeypatch.setattr(OpenAIQueryNormalizer, "normalize", _fail_normalization)
    _, token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/search", json={"query": "cat"}, headers=headers)).status_code == 503

    unfilled_openai_secret["values"][unfilled_openai_secret["arn"]] = "sk-now-set"
    first = await client.post("/search", json={"query": "cat"}, headers=headers)
    second = await client.post("/search", json={"query": "dog"}, headers=headers)

    assert (first.status_code, second.status_code) == (200, 200)
    assert _FakeOpenAIEmbedder.created_with == ["sk-now-set"]
    # Once when it had no value, once when it did; never again.
    assert unfilled_openai_secret["fetched"] == [unfilled_openai_secret["arn"]] * 2


async def _fail_normalization(self, query: str) -> str:
    raise ConnectionError("not under test")
