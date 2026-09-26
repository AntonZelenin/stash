"""Search against a real Postgres + pgvector: the queries the SQLite suite
has to stub (best chunk per item, full-text, trigram), on the schema the
migrations build. Each run creates a throwaway database on the server
`STASH_TEST_POSTGRES_URL` points at (a user allowed to create databases
and the `vector` extension, e.g. the docker compose one:
`postgresql+asyncpg://stash:stash@localhost:5432/postgres`), migrates it
to head and drops it afterwards. Skipped when that's not set."""

import asyncio
import logging
import math
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from stash_shared.embeddings import EMBEDDING_DIMENSIONS, Embedder

from app.items.models import Description, ItemStatus, ItemType, SearchChunk
from app.items.repos import ItemRepository
from app.items.services import ItemService
from app.query_normalization import QueryNormalizer
from app.users.models import User

_ADMIN_URL = os.environ.get("STASH_TEST_POSTGRES_URL")
_API_DIR = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(not _ADMIN_URL, reason="STASH_TEST_POSTGRES_URL is not set")


def _unit(**components: float) -> list[float]:
    """A unit vector with the given components on named axes (`a0`, `a1`,
    ...), so tests can set up exact cosine distances: two unit vectors'
    distance is 1 minus their dot product."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for axis, value in components.items():
        vector[int(axis.removeprefix("a"))] = value
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


def _at_distance(distance: float, *, towards: str, away: str) -> list[float]:
    """A unit vector exactly `distance` from the unit vector on axis
    `towards`, turned into axis `away`."""
    similarity = 1 - distance
    return _unit(**{towards: similarity, away: math.sqrt(1 - similarity * similarity)})


def _to_text(vector: list[float]) -> str:
    return "[" + ",".join(repr(value) for value in vector) + "]"


async def _execute_as_admin(statement: str) -> None:
    # CREATE/DROP DATABASE can't run in a transaction: plain asyncpg.
    url = make_url(_ADMIN_URL).set(drivername="postgresql")
    conn = await asyncpg.connect(url.render_as_string(hide_password=False))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def database_url() -> URL:
    name = f"stash_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_execute_as_admin(f'CREATE DATABASE "{name}"'))
    url = make_url(_ADMIN_URL).set(database=name)
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_API_DIR,
            env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
            check=True,
            capture_output=True,
        )
        yield url
    finally:
        asyncio.run(_execute_as_admin(f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture
async def session(database_url: URL) -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def user_id(session: AsyncSession) -> uuid.UUID:
    """A new user for each test, so tests don't see each other's items."""
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    session.add(user)
    await session.commit()
    return user.id


class _QueryEmbedder(Embedder):
    """Embeds each query as `vectors[query]`."""

    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.vectors[text] for text in texts]


class _AsTyped(QueryNormalizer):
    async def normalize(self, query: str) -> str:
        return query


class _Storage:
    async def generate_download_url(self, *, key: str, **kwargs) -> str:
        return f"https://storage.test/{key}"


async def _image(session: AsyncSession, user_id: uuid.UUID, chunks: dict[str, list[float]]) -> uuid.UUID:
    """A completed image whose generated description is `chunks` (one per
    line, as the image analyzer stores them), embedded as given — what the
    embedding worker would have written."""
    item_id = uuid.uuid4()
    repo = ItemRepository(session)
    item = await repo.create_image_item(
        item_id=item_id, user_id=user_id, storage_key=f"images/{item_id}.png", content_type="image/png", size_bytes=1
    )
    item.status = ItemStatus.completed
    item.description = Description(text="\n".join(chunks))
    session.add_all(
        SearchChunk(item_id=item_id, position=position, text=text, embedding=_to_text(vector))
        for position, (text, vector) in enumerate(chunks.items())
    )
    await session.commit()
    return item_id


async def _search(session: AsyncSession, user_id: uuid.UUID, query: str, vector: list[float]) -> list[uuid.UUID]:
    results = await ItemService(session, _Storage()).search_items(
        user_id=user_id, query=query, limit=20, embedder=_QueryEmbedder({query: vector}), normalizer=_AsTyped()
    )
    return [listed.item.id for listed in results]


async def test_an_item_is_as_near_as_its_best_chunk_and_listed_once(session, user_id):
    wallpaper = await _image(
        session,
        user_id,
        {
            "cyberpunk city, futuristic urban environment": _unit(a1=1),
            "woman, girl, female": _unit(a0=1),
            "anime girl, young woman": _at_distance(0.1, towards="a0", away="a2"),
        },
    )
    landscape = await _image(session, user_id, {"mountain lake": _at_distance(0.5, towards="a0", away="a3")})

    matches = await ItemRepository(session).search_by_chunks(user_id=user_id, query_embedding=_unit(a0=1), limit=10)

    assert [(match.item.id, match.chunk_text) for match in matches] == [
        (wallpaper, "woman, girl, female"),
        (landscape, "mountain lake"),
    ]
    assert matches[0].distance == pytest.approx(0.0, abs=1e-6)
    assert matches[1].distance == pytest.approx(0.5, abs=1e-6)


async def test_only_the_users_own_items_are_searched(session, user_id):
    other_user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    session.add(other_user)
    await session.commit()
    await _image(session, other_user.id, {"woman, girl, female": _unit(a0=1)})

    assert await ItemRepository(session).search_by_chunks(user_id=user_id, query_embedding=_unit(a0=1), limit=10) == []


async def test_the_cutoff_applies_to_the_best_chunk(session, user_id):
    """0.6 (`search_max_cosine_distance`): an item is in if its best chunk
    is, however far its other chunks are."""
    near = await _image(
        session,
        user_id,
        {
            "street at night": _at_distance(0.95, towards="a0", away="a1"),
            "girl with umbrella": _at_distance(0.55, towards="a0", away="a2"),
        },
    )
    await _image(session, user_id, {"bowl of fruit": _at_distance(0.65, towards="a0", away="a3")})

    assert await _search(session, user_id, "girl", _unit(a0=1)) == [near]


async def test_girl_and_city_find_the_image_whose_chunks_mention_them(session, user_id, caplog):
    """The regression this replaced whole-description embeddings for: a
    short query close to one chunk finds the image, however much else the
    image shows."""
    caplog.set_level(logging.INFO)
    wallpaper = await _image(
        session,
        user_id,
        {
            "woman, girl, female": _unit(a0=1),
            "cyberpunk city, futuristic urban environment": _unit(a1=1),
            "white shirt, dark shorts, black shoes": _unit(a2=1),
        },
    )

    assert await _search(session, user_id, "girl", _at_distance(0.4, towards="a0", away="a5")) == [wallpaper]
    assert await _search(session, user_id, "city", _at_distance(0.4, towards="a1", away="a5")) == [wallpaper]
    # "city" is also literally in the text.
    last_result = [record for record in caplog.records if record.getMessage() == "Search result"][-1]
    assert last_result.stash_fields["match_sources"] == ["description", "semantic"]


async def test_a_literal_word_in_a_chunk_matches_however_far_its_embedding(session, user_id, caplog):
    caplog.set_level(logging.INFO)
    wallpaper = await _image(
        session,
        user_id,
        {"woman, girl, female": _unit(a0=1), "cyberpunk city, futuristic urban environment": _unit(a1=1)},
    )

    # Nowhere near any chunk by meaning, but in the text ("cities" stems
    # to "city").
    assert await _search(session, user_id, "cities", _unit(a7=1)) == [wallpaper]
    [candidate] = [record for record in caplog.records if record.getMessage() == "Semantic search candidate"]
    assert candidate.stash_fields["passed_threshold"] is False
    [result] = [record for record in caplog.records if record.getMessage() == "Search result"]
    assert result.stash_fields["match_sources"] == ["description"]


async def test_filenames_and_the_users_own_text_are_still_matched(session, user_id):
    repo = ItemRepository(session)
    resume = await repo.create_file_item(
        item_id=uuid.uuid4(),
        user_id=user_id,
        storage_key="files/resume.pdf",
        filename="Resume-2.pdf",
        content_type="application/pdf",
        size_bytes=1,
    )
    note = await repo.create_text_item(user_id=user_id, text="buy oat milk today", item_type=ItemType.text)
    await session.commit()

    assert await _search(session, user_id, "resume 2", _unit(a7=1)) == [resume.id]
    assert await _search(session, user_id, "milk", _unit(a7=1)) == [note.id]


async def test_deleting_an_item_deletes_its_chunks(session, user_id):
    item_id = await _image(session, user_id, {"grey cat": _unit(a0=1), "sofa": _unit(a1=1)})

    await ItemRepository(session).delete_item(item_id=item_id, user_id=user_id)
    await session.commit()

    count = await session.scalar(select(func.count()).select_from(SearchChunk).where(SearchChunk.item_id == item_id))
    assert count == 0
