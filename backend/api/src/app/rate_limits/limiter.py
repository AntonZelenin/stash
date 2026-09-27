"""Rate limits and per-user quotas, shared by every API instance.

A `Limit` is one or more fixed windows ("5/15m,20/1d", see
`app.rate_limits.windows`); a request is allowed only if it fits in every
one. Counters live in Postgres (`rate_limit_counters`), the one datastore
every API instance (each Lambda execution environment, each uvicorn worker)
shares, so a limit holds however many instances serve requests. Nothing is
kept in process memory.

`RateLimiter.consume` charges one or more limits atomically: each window's
counter is one conditional upsert that adds the cost only if the result
stays within the limit, all of them in one transaction that's rolled back
if any window is full. So concurrent requests can't overshoot a limit
between reading and writing a counter, a request is never charged for one
limit when another rejects it, and a rejected request charges nothing:
hammering a full limit doesn't push its reset further out. Counters are
written in key order, so two requests charging the same limits can't
deadlock.

Counters are written on their own connection, in their own transaction,
never the request's: a request that fails afterwards (a wrong password,
a validation error) must still have been counted.

Charging before the work also serves "reserve, then refund": login reserves
a failed attempt before checking the password and refunds it when the
password was right, so parallel guesses can't all be checked before any of
them is counted.

Windows are fixed periods (aligned to multiples of their length), which is
what makes a counter one row and one statement. The price is the boundary
effect: a client can use a window's whole limit at the end of one period
and again at the start of the next, so up to about twice the limit can pass
within one window's length around a rollover. A limit with several windows
bounds that by its longer ones (e.g. "5/5m,10/30m": at most 10 in 5
minutes around a rollover, but still 10 in the half hour).

Cost: a limited request runs one short transaction, one upsert per window
(each a primary-key lookup and an in-place update), on a connection of its
own. Requests for the same subject and limit serialize on that row, only
for the upsert. About 1% of charges also delete up to `_PRUNE_BATCH`
counters of ended periods (via their `expires_at` index), skipping any a
charge has locked, so the table holds roughly one row per active subject
and window.
"""

import hashlib
import ipaddress
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from fastapi import Request
from sqlalchemy import BigInteger, case, delete, literal, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from stash_shared.log import get_logger

from app.config import Settings, get_settings
from app.db import engine
from app.rate_limits.models import RateLimitCounter
from app.rate_limits.windows import Window, parse_windows

logger = get_logger(__name__)

_COUNTERS = RateLimitCounter.__table__
# Share of `consume` calls that also delete counters of periods that have
# ended, so the table doesn't grow without bound, and how many at most each
# time, so no request pays for a large delete.
_PRUNE_PROBABILITY = 0.01
_PRUNE_BATCH = 1_000
_DAY_SECONDS = 86_400


class RateLimitExceeded(Exception):
    """A request doesn't fit in a limit. `retry_after_seconds`: when the
    earliest period that would let it through ends (sent as Retry-After).
    `limit_name` is for logs only, never for the client."""

    def __init__(self, *, retry_after_seconds: int, limit_name: str):
        super().__init__(f"rate limit {limit_name!r} exceeded")
        self.retry_after_seconds = retry_after_seconds
        self.limit_name = limit_name


@dataclass(frozen=True)
class Limit:
    # Identifies the limit's counters; changing it resets them.
    name: str
    # Empty: unlimited.
    windows: tuple[Window, ...]

    @classmethod
    def parse(cls, name: str, spec: str) -> "Limit":
        return cls(name=name, windows=parse_windows(spec))

    @property
    def per_day(self) -> int | None:
        """The limit of its one-day window, if it has one."""
        return next((window.limit for window in self.windows if window.seconds == _DAY_SECONDS), None)


@dataclass(frozen=True)
class Charge:
    """`cost` units of `limit` for `subject` (a user id, an IP, an email...;
    never stored as such, only hashed)."""

    limit: Limit
    subject: str
    cost: int = 1


@dataclass(frozen=True)
class RateLimits:
    """Every limit the API enforces; see the settings of the same names
    for what each counts and its defaults."""

    login_per_ip: Limit
    login_failures_per_account_ip: Limit
    login_failures_per_account: Limit
    registration_per_ip: Limit
    registration_per_email: Limit
    token_refresh_per_ip: Limit
    password_change_failures_per_user: Limit
    uploads_per_user: Limit
    upload_bytes_per_user: Limit
    ai_analyses_per_user: Limit
    searches_per_user: Limit
    item_writes_per_user: Limit

    @classmethod
    def from_settings(cls, settings: Settings) -> "RateLimits":
        return cls(
            login_per_ip=Limit.parse("login_ip", settings.login_limit_per_ip),
            login_failures_per_account_ip=Limit.parse(
                "login_failures_account_ip", settings.login_failure_limit_per_account_ip
            ),
            login_failures_per_account=Limit.parse(
                "login_failures_account", settings.login_failure_limit_per_account
            ),
            registration_per_ip=Limit.parse("registration_ip", settings.registration_limit_per_ip),
            registration_per_email=Limit.parse("registration_email", settings.registration_limit_per_email),
            token_refresh_per_ip=Limit.parse("token_refresh_ip", settings.token_refresh_limit_per_ip),
            password_change_failures_per_user=Limit.parse(
                "password_change_failures_user", settings.password_change_failure_limit_per_user
            ),
            uploads_per_user=Limit.parse("uploads_user", settings.upload_limit_per_user),
            upload_bytes_per_user=Limit.parse("upload_bytes_user", settings.upload_bytes_quota_per_user),
            ai_analyses_per_user=Limit.parse("ai_analyses_user", settings.ai_analysis_quota_per_user),
            searches_per_user=Limit.parse("searches_user", settings.search_limit_per_user),
            item_writes_per_user=Limit.parse("item_writes_user", settings.item_write_limit_per_user),
        )


@dataclass(frozen=True)
class _Counter:
    key: str
    limit_name: str
    window: Window
    window_start: int
    cost: int


class RateLimiter:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        limits: RateLimits,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
        prune_probability: float = _PRUNE_PROBABILITY,
    ):
        """`engine`: the database the counters are in; each call uses a
        connection of its own. `enabled=False` allows everything and
        touches nothing. `clock`: Unix time in seconds (tests move it)."""
        self.limits = limits
        self.enabled = enabled
        self._engine = engine
        self._clock = clock
        self._prune_probability = prune_probability

    async def consume(self, *charges: Charge) -> None:
        """Charges every one of `charges`, or none of them: raises
        `RateLimitExceeded` if any window of any of them would go over its
        limit."""
        if not self.enabled:
            return
        now = self._clock()
        counters = self._counters(charges, now)
        if not counters:
            return
        blocked: list[tuple[float, str]] = []
        async with self._engine.begin() as conn:
            for counter in counters:
                if not await self._try_add(conn, counter, now):
                    blocked.append((counter.window_start + counter.window.seconds - now, counter.limit_name))
            if blocked:
                # Raised inside the transaction, which rolls it back: none
                # of the counters it did add stay added.
                retry_after, limit_name = max(blocked)
                raise RateLimitExceeded(retry_after_seconds=max(1, math.ceil(retry_after)), limit_name=limit_name)
        if random.random() < self._prune_probability:
            await self._prune(now)

    async def refund(self, *charges: Charge) -> None:
        """Takes back `charges` consumed earlier (e.g. a failed login
        reserved before the password turned out right). Only from the
        periods they were charged in: once a period ended, there's nothing
        to take back. Never below zero."""
        if not self.enabled:
            return
        counters = self._counters(charges, self._clock())
        if not counters:
            return
        async with self._engine.begin() as conn:
            for counter in counters:
                await conn.execute(
                    update(_COUNTERS)
                    .where(_COUNTERS.c.key == counter.key, _COUNTERS.c.window_start == counter.window_start)
                    .values(
                        count=case(
                            (_COUNTERS.c.count > counter.cost, _COUNTERS.c.count - counter.cost),
                            else_=literal(0, BigInteger),
                        )
                    )
                )

    @staticmethod
    def _counters(charges: tuple[Charge, ...], now: float) -> list[_Counter]:
        """One counter per window of each charge, costs of the same counter
        added up, in key order (the order they're locked in)."""
        by_key: dict[str, _Counter] = {}
        for charge in charges:
            if charge.cost <= 0:
                continue
            subject = hashlib.sha256(charge.subject.encode()).hexdigest()[:32]
            for window in charge.limit.windows:
                key = f"{charge.limit.name}:{window.seconds}:{subject}"
                previous = by_key.get(key)
                by_key[key] = _Counter(
                    key=key,
                    limit_name=charge.limit.name,
                    window=window,
                    window_start=int(now // window.seconds) * window.seconds,
                    cost=charge.cost + (previous.cost if previous else 0),
                )
        return [by_key[key] for key in sorted(by_key)]

    @staticmethod
    async def _try_add(conn: AsyncConnection, counter: _Counter, now: float) -> bool:
        """Adds `counter.cost` to its counter if the result stays within
        the window's limit, starting the count over if the stored period
        has ended. One statement: the row is locked from the check to the
        write. Whether it was added."""
        if counter.cost > counter.window.limit:
            # Never fits, even in an empty period.
            return False
        insert = postgresql.insert if conn.dialect.name == "postgresql" else sqlite.insert
        expires_at = datetime.fromtimestamp(counter.window_start + counter.window.seconds, UTC)
        # The stored period is still the current one; ">=": another
        # instance whose clock is slightly ahead may already have started
        # the next, which then counts as current.
        is_current = _COUNTERS.c.window_start >= counter.window_start
        used = case((is_current, _COUNTERS.c.count), else_=literal(0, BigInteger))
        statement = (
            insert(_COUNTERS)
            .values(key=counter.key, window_start=counter.window_start, count=counter.cost, expires_at=expires_at)
            .on_conflict_do_update(
                index_elements=[_COUNTERS.c.key],
                set_={
                    "count": used + counter.cost,
                    "window_start": case((is_current, _COUNTERS.c.window_start), else_=counter.window_start),
                    "expires_at": case((is_current, _COUNTERS.c.expires_at), else_=expires_at),
                },
                where=used + counter.cost <= counter.window.limit,
            )
            .returning(_COUNTERS.c.count)
        )
        return (await conn.execute(statement)).first() is not None

    async def _prune(self, now: float) -> None:
        """Deletes up to `_PRUNE_BATCH` counters of ended periods, oldest
        first. On Postgres, skipping rows another transaction has locked:
        a charge starting a new period in one isn't waited for (its row is
        current again anyway). Best effort: a failure only leaves old rows
        for the next time."""
        try:
            async with self._engine.begin() as conn:
                expired = (
                    select(_COUNTERS.c.key)
                    .where(_COUNTERS.c.expires_at < datetime.fromtimestamp(now, UTC))
                    .order_by(_COUNTERS.c.expires_at)
                    .limit(_PRUNE_BATCH)
                )
                if conn.dialect.name == "postgresql":
                    expired = expired.with_for_update(skip_locked=True)
                await conn.execute(delete(_COUNTERS).where(_COUNTERS.c.key.in_(expired.scalar_subquery())))
        except Exception:
            logger.warning("Failed to prune expired rate limit counters", exc_info=True)


def client_ip(request: Request) -> str:
    """The IP address per-IP limits count by: the connection's peer as the
    server saw it, never a client-supplied header. `X-Forwarded-For` in
    particular is ignored: anyone can send one. On AWS, Mangum fills the
    peer from API Gateway's `requestContext.http.sourceIp`, the address
    that connected to API Gateway. Locally, uvicorn only honours forwarded
    headers from 127.0.0.1.

    IPv6 addresses are counted by their /64 network: one host is usually
    given a whole /64, and could otherwise use a fresh address per request.
    """
    host = request.client.host if request.client else None
    if not host:
        return "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


@lru_cache
def get_rate_limiter() -> RateLimiter:
    settings = get_settings()
    return RateLimiter(
        engine=engine,
        limits=RateLimits.from_settings(settings),
        enabled=settings.rate_limits_enabled,
    )
