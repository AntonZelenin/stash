"""Checks the real model's rewrites, against OpenAI (costs a few cents).
Skipped unless opted in:

    STASH_LIVE_OPENAI_TESTS=1 OPENAI_API_KEY=sk-... pytest tests/live

Uses `SEARCH_QUERY_NORMALIZATION_MODEL` / `EMBEDDING_MODEL` if set, like
the API."""

import math
import os
import string

import pytest
from stash_shared.embeddings import OpenAIEmbedder

from app.config import Settings
from app.query_normalization import OpenAIQueryNormalizer

pytestmark = pytest.mark.skipif(
    os.environ.get("STASH_LIVE_OPENAI_TESTS") != "1" or not os.environ.get("OPENAI_API_KEY"),
    reason="live OpenAI tests: set STASH_LIVE_OPENAI_TESTS=1 and OPENAI_API_KEY",
)

# Rewrites of equivalent queries must embed at least this close together
# (cosine similarity). Unrelated short phrases score well under 0.5.
_MIN_EQUIVALENT_SIMILARITY = 0.85


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def normalizer(settings: Settings) -> OpenAIQueryNormalizer:
    return OpenAIQueryNormalizer(
        api_key=settings.openai_api_key,
        model=settings.search_query_normalization_model,
        timeout_seconds=30,
    )


@pytest.fixture
def embedder(settings: Settings) -> OpenAIEmbedder:
    return OpenAIEmbedder(api_key=settings.openai_api_key, model=settings.embedding_model)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def _comparable(text: str) -> str:
    return text.casefold().strip().strip(string.punctuation)


@pytest.mark.parametrize(
    ("ukrainian", "english"),
    [
        ("дівчина", "girl"),
        ("дівчина в помаранчевому светрі", "girl in an orange sweater"),
        ("книга про магію", "book about magic"),
        ("кіт спить на дивані", "cat sleeping on a sofa"),
        ("рецепт борщу", "borscht recipe"),
    ],
)
async def test_equivalent_ukrainian_and_english_queries_normalize_alike(
    normalizer: OpenAIQueryNormalizer, embedder: OpenAIEmbedder, ukrainian: str, english: str
):
    from_ukrainian = await normalizer.normalize(ukrainian)
    from_english = await normalizer.normalize(english)

    similarity = _cosine_similarity(await embedder.embed(from_ukrainian), await embedder.embed(from_english))
    assert similarity >= _MIN_EQUIVALENT_SIMILARITY, (from_ukrainian, from_english, similarity)
    # Actually translated, not passed through.
    assert from_ukrainian.isascii(), from_ukrainian


@pytest.mark.parametrize(
    "query",
    [
        "girl in orange sweater",
        "book about magic",
        "tax documents 2024",
        "python asyncio tutorial",
        "cat",
    ],
)
async def test_english_queries_are_kept_as_they_are(normalizer: OpenAIQueryNormalizer, query: str):
    assert _comparable(await normalizer.normalize(query)) == _comparable(query)
