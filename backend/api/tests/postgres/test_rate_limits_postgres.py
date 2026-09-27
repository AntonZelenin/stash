"""The rate limiter against a real Postgres, on the schema the migrations
build: the conditional upsert as asyncpg runs it, and counters holding
under truly concurrent transactions (many connections, as many API
instances would use). Skipped when `STASH_TEST_POSTGRES_URL` isn't set
(see `conftest`)."""

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncGenerator
from dataclasses import fields

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.rate_limits.limiter import Charge, Limit, RateLimiter, RateLimitExceeded, RateLimits
from app.rate_limits.windows import parse_windows

pytestmark = pytest.mark.skipif(not os.environ.get("STASH_TEST_POSTGRES_URL"), reason="STASH_TEST_POSTGRES_URL is not set")


@pytest.fixture
async def engine(database_url: URL) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(database_url, pool_size=20)
    yield engine
    await engine.dispose()


class _Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def _limiter(engine: AsyncEngine, clock: _Clock) -> RateLimiter:
    limits = RateLimits(**{field.name: Limit(field.name, ()) for field in fields(RateLimits)})
    return RateLimiter(engine=engine, limits=limits, clock=clock, prune_probability=0)


async def _allowed(limiter: RateLimiter, *charges: Charge) -> bool:
    try:
        await limiter.consume(*charges)
    except RateLimitExceeded:
        return False
    return True


async def test_limits_resets_and_refunds(engine: AsyncEngine):
    clock = _Clock()
    limiter = _limiter(engine, clock)
    limit = Limit("pg", parse_windows("2/1m,3/1h"))
    subject = uuid.uuid4().hex

    assert [await _allowed(limiter, Charge(limit, subject)) for _ in range(3)] == [True, True, False]
    await limiter.refund(Charge(limit, subject))
    assert await _allowed(limiter, Charge(limit, subject))

    clock.now += 60
    # A new minute; the hour has 2 used (the refunded one isn't).
    assert await _allowed(limiter, Charge(limit, subject))
    assert not await _allowed(limiter, Charge(limit, subject))
    clock.now += 3600
    assert await _allowed(limiter, Charge(limit, subject))


async def test_byte_quotas_beyond_32_bits(engine: AsyncEngine):
    limiter = _limiter(engine, _Clock())
    quota = Limit("pg_bytes", parse_windows(f"{5 * 1024**3}/1d"))
    subject = uuid.uuid4().hex

    assert await _allowed(limiter, Charge(quota, subject, cost=3 * 1024**3))
    assert not await _allowed(limiter, Charge(quota, subject, cost=3 * 1024**3))
    assert await _allowed(limiter, Charge(quota, subject, cost=2 * 1024**3))


async def test_concurrent_requests_never_exceed_the_limit(engine: AsyncEngine):
    limiter = _limiter(engine, _Clock())
    limit = Limit("pg_concurrent", parse_windows("10/1m"))
    subject = uuid.uuid4().hex

    results = await asyncio.gather(*(_allowed(limiter, Charge(limit, subject)) for _ in range(50)))

    assert sum(results) == 10


async def test_concurrent_multi_limit_charges_neither_overshoot_nor_deadlock(engine: AsyncEngine):
    """Requests charging the same two limits, listed in opposite orders:
    locked in key order, so they can't deadlock, and all-or-nothing, so
    neither limit goes over."""
    limiter = _limiter(engine, _Clock())
    first = Limit("pg_first", parse_windows("15/1m"))
    second = Limit("pg_second", parse_windows("15/1m"))
    subject = uuid.uuid4().hex

    results = await asyncio.gather(
        *(
            _allowed(limiter, Charge(first, subject), Charge(second, subject))
            if index % 2
            else _allowed(limiter, Charge(second, subject), Charge(first, subject))
            for index in range(40)
        )
    )

    assert sum(results) == 15


async def test_pruning_skips_counters_in_use_instead_of_waiting(engine: AsyncEngine):
    clock = _Clock()
    limiter = _limiter(engine, clock)
    limit = Limit("pg_prune", parse_windows("5/1m"))
    locked, free = uuid.uuid4().hex, uuid.uuid4().hex
    for subject in (locked, free):
        await limiter.consume(Charge(limit, subject))
    clock.now += 120

    async with engine.connect() as holder:
        await holder.begin()
        # A charge's transaction holding one expired counter's row.
        await holder.execute(
            text("SELECT key FROM rate_limit_counters WHERE key LIKE :key FOR UPDATE"),
            {"key": f"pg_prune:%:{hashlib.sha256(locked.encode()).hexdigest()[:32]}"},
        )
        await asyncio.wait_for(limiter._prune(clock.now), timeout=5)
        await holder.rollback()

    async with engine.connect() as conn:
        keys = (await conn.execute(text("SELECT key FROM rate_limit_counters WHERE key LIKE 'pg_prune:%'"))).scalars().all()
    assert [key.rsplit(":", 1)[1] for key in keys] == [hashlib.sha256(locked.encode()).hexdigest()[:32]]


async def test_counter_table_leaves_room_for_in_place_updates(engine: AsyncEngine):
    async with engine.connect() as conn:
        options = (
            await conn.execute(text("SELECT reloptions FROM pg_class WHERE relname = 'rate_limit_counters'"))
        ).scalar_one()

    assert "fillfactor=70" in options
