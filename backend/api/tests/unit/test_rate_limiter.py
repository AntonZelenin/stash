import asyncio
from dataclasses import fields
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.rate_limits import limiter as limiter_module
from app.rate_limits.limiter import (
    Charge,
    Limit,
    RateLimiter,
    RateLimitExceeded,
    RateLimits,
    client_ip,
)
from app.rate_limits.models import RateLimitCounter
from app.rate_limits.windows import Window, parse_windows

# A multiple of every window length used below, so each test starts at the
# beginning of a period.
_START = 1_800_000_000 - 1_800_000_000 % 86_400


class FakeClock:
    def __init__(self, now: float = _START):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def engine(tmp_path):
    # A file, not :memory:, so concurrent transactions get connections of
    # their own and contend for the database like on Postgres.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'limits.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(RateLimitCounter.metadata.create_all, tables=[RateLimitCounter.__table__])
    yield engine
    await engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _limiter(engine, clock, **kwargs) -> RateLimiter:
    limits = RateLimits(**{field.name: Limit(field.name, ()) for field in fields(RateLimits)})
    return RateLimiter(engine=engine, limits=limits, clock=clock, prune_probability=0, **kwargs)


async def _blocked(limiter: RateLimiter, *charges: Charge) -> RateLimitExceeded | None:
    try:
        await limiter.consume(*charges)
    except RateLimitExceeded as exc:
        return exc
    return None


# ---- windows ----


def test_parses_windows_shortest_first():
    assert parse_windows("20/1d, 5/15m,10/30s") == (Window(10, 30), Window(5, 900), Window(20, 86_400))


def test_empty_spec_is_unlimited():
    assert parse_windows("") == ()


@pytest.mark.parametrize("spec", ["5", "5/15", "5/15x", "-1/1m", "5/0m", "5/1m,6/60s", "five/1m"])
def test_rejects_malformed_specs(spec: str):
    with pytest.raises(ValueError):
        parse_windows(spec)


def test_settings_reject_malformed_limits():
    with pytest.raises(ValueError):
        Settings(login_limit_per_ip="lots")


def test_default_settings_parse():
    limits = RateLimits.from_settings(Settings())
    assert limits.login_per_ip.windows[0] == Window(20, 60)
    assert limits.upload_bytes_per_user.windows == (Window(5 * 1024**3, 86_400),)


# ---- consume ----


async def test_allows_up_to_the_limit_then_blocks_with_retry_after(engine, clock):
    limiter = _limiter(engine, clock)
    limit = Limit("test", parse_windows("3/1m"))
    clock.now += 20

    for _ in range(3):
        await limiter.consume(Charge(limit, "alice"))
    blocked = await _blocked(limiter, Charge(limit, "alice"))

    assert blocked is not None
    # The period started 20 s ago and lasts 60.
    assert blocked.retry_after_seconds == 40
    assert blocked.limit_name == "test"


async def test_resets_when_the_period_ends(engine, clock):
    limiter = _limiter(engine, clock)
    limit = Limit("test", parse_windows("2/1m"))
    await limiter.consume(Charge(limit, "alice"))
    await limiter.consume(Charge(limit, "alice"))
    assert await _blocked(limiter, Charge(limit, "alice")) is not None

    clock.now += 60

    await limiter.consume(Charge(limit, "alice"))
    await limiter.consume(Charge(limit, "alice"))
    assert await _blocked(limiter, Charge(limit, "alice")) is not None


async def test_subjects_and_limits_are_counted_separately(engine, clock):
    limiter = _limiter(engine, clock)
    first = Limit("first", parse_windows("1/1m"))
    second = Limit("second", parse_windows("1/1m"))
    await limiter.consume(Charge(first, "alice"))

    await limiter.consume(Charge(first, "bob"))
    await limiter.consume(Charge(second, "alice"))
    assert await _blocked(limiter, Charge(first, "alice")) is not None


async def test_every_window_must_have_room(engine, clock):
    limiter = _limiter(engine, clock)
    limit = Limit("test", parse_windows("2/1m,3/1h"))
    await limiter.consume(Charge(limit, "alice"))
    await limiter.consume(Charge(limit, "alice"))
    clock.now += 60
    await limiter.consume(Charge(limit, "alice"))

    # The minute has room again, the hour doesn't: retry when the hour ends.
    blocked = await _blocked(limiter, Charge(limit, "alice"))
    assert blocked is not None
    assert blocked.retry_after_seconds == 3600 - 60


async def test_blocked_requests_are_not_counted(engine, clock):
    """Hammering a full limit must not push its reset further out, nor use
    up a longer window."""
    limiter = _limiter(engine, clock)
    limit = Limit("test", parse_windows("1/1m,3/1h"))
    await limiter.consume(Charge(limit, "alice"))
    for _ in range(10):
        assert await _blocked(limiter, Charge(limit, "alice")) is not None

    clock.now += 60
    await limiter.consume(Charge(limit, "alice"))
    clock.now += 60
    await limiter.consume(Charge(limit, "alice"))


async def test_charges_are_all_or_nothing(engine, clock):
    limiter = _limiter(engine, clock)
    roomy = Limit("roomy", parse_windows("10/1m"))
    full = Limit("full", parse_windows("1/1m"))
    await limiter.consume(Charge(full, "alice"))

    for _ in range(5):
        assert await _blocked(limiter, Charge(roomy, "alice"), Charge(full, "alice")) is not None

    # None of the rejected attempts used up `roomy`.
    for _ in range(10):
        await limiter.consume(Charge(roomy, "alice"))


async def test_costs_are_counted_in_units(engine, clock):
    limiter = _limiter(engine, clock)
    quota = Limit("bytes", parse_windows("100/1d"))
    await limiter.consume(Charge(quota, "alice", cost=60))

    assert await _blocked(limiter, Charge(quota, "alice", cost=41)) is not None
    await limiter.consume(Charge(quota, "alice", cost=40))


async def test_a_cost_over_the_whole_limit_never_fits(engine, clock):
    limiter = _limiter(engine, clock)
    quota = Limit("bytes", parse_windows("100/1d"))

    assert await _blocked(limiter, Charge(quota, "alice", cost=101)) is not None
    await limiter.consume(Charge(quota, "alice", cost=100))


async def test_zero_cost_and_unlimited_charges_touch_nothing(engine, clock):
    limiter = _limiter(engine, clock)
    await limiter.consume(Charge(Limit("test", parse_windows("1/1m")), "alice", cost=0))
    await limiter.consume(Charge(Limit("unlimited", ()), "alice"))

    async with engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(RateLimitCounter))).scalar_one() == 0


async def test_subjects_are_stored_hashed(engine, clock):
    limiter = _limiter(engine, clock)
    await limiter.consume(Charge(Limit("test", parse_windows("1/1m")), "alice@example.com"))

    async with engine.connect() as conn:
        [key] = (await conn.execute(select(RateLimitCounter.key))).scalars()
    assert "alice" not in key
    assert key.startswith("test:60:")


async def test_refund_gives_back_what_was_charged(engine, clock):
    limiter = _limiter(engine, clock)
    limit = Limit("test", parse_windows("2/1m"))
    for _ in range(5):
        await limiter.consume(Charge(limit, "alice"))
        await limiter.refund(Charge(limit, "alice"))

    await limiter.consume(Charge(limit, "alice"))
    await limiter.consume(Charge(limit, "alice"))
    assert await _blocked(limiter, Charge(limit, "alice")) is not None


async def test_refund_never_goes_below_zero_or_into_another_period(engine, clock):
    limiter = _limiter(engine, clock)
    limit = Limit("test", parse_windows("1/1m"))
    await limiter.refund(Charge(limit, "alice"))
    await limiter.consume(Charge(limit, "alice"))
    clock.now += 60
    await limiter.consume(Charge(limit, "alice"))
    await limiter.refund(Charge(limit, "alice"), Charge(limit, "alice"))

    await limiter.consume(Charge(limit, "alice"))
    assert await _blocked(limiter, Charge(limit, "alice")) is not None


async def test_disabled_limiter_allows_everything(engine, clock):
    limiter = _limiter(engine, clock, enabled=False)
    limit = Limit("test", parse_windows("1/1m"))
    for _ in range(5):
        await limiter.consume(Charge(limit, "alice"))


async def test_prunes_counters_of_ended_periods(engine, clock):
    limiter = _limiter(engine, clock)
    await limiter.consume(Charge(Limit("minute", parse_windows("5/1m")), "alice"))
    await limiter.consume(Charge(Limit("day", parse_windows("5/1d")), "alice"))
    clock.now += 120

    await limiter._prune(clock.now)

    async with engine.connect() as conn:
        keys = (await conn.execute(select(RateLimitCounter.key))).scalars().all()
    assert [key.split(":")[0] for key in keys] == ["day"]


async def test_prunes_at_most_a_batch_at_a_time_oldest_first(engine, clock, monkeypatch):
    monkeypatch.setattr(limiter_module, "_PRUNE_BATCH", 2)
    limiter = _limiter(engine, clock)
    for name in "abcde":
        await limiter.consume(Charge(Limit("minute", parse_windows("5/1m")), name))
    await limiter.consume(Charge(Limit("hour", parse_windows("5/1h")), "a"))
    clock.now += 7_200

    remaining = []
    for _ in range(4):
        await limiter._prune(clock.now)
        async with engine.connect() as conn:
            remaining.append(len((await conn.execute(select(RateLimitCounter.key))).all()))

    assert remaining == [4, 2, 0, 0]


async def test_concurrent_requests_never_exceed_the_limit(engine, clock):
    limiter = _limiter(engine, clock)
    limit = Limit("test", parse_windows("10/1m"))

    results = await asyncio.gather(*(_blocked(limiter, Charge(limit, "alice")) for _ in range(40)))

    assert sum(result is None for result in results) == 10


# ---- client IP ----


def _request(host: str | None, headers: dict[str, str] | None = None):
    return SimpleNamespace(client=SimpleNamespace(host=host) if host else None, headers=headers or {})


def test_client_ip_ignores_forwarded_headers():
    request = _request("203.0.113.7", {"x-forwarded-for": "198.51.100.1", "x-real-ip": "198.51.100.2"})
    assert client_ip(request) == "203.0.113.7"


def test_client_ip_groups_ipv6_by_64():
    assert client_ip(_request("2001:db8:1:2:aaaa::1")) == client_ip(_request("2001:db8:1:2:bbbb::9"))
    assert client_ip(_request("2001:db8:1:2::1")) == "2001:db8:1:2::/64"
    assert client_ip(_request("2001:db8:1:3::1")) != client_ip(_request("2001:db8:1:2::1"))


def test_client_ip_unmaps_ipv4_in_ipv6():
    assert client_ip(_request("::ffff:203.0.113.7")) == "203.0.113.7"


def test_client_ip_without_a_peer():
    assert client_ip(_request(None)) == "unknown"
