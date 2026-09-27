from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RateLimitCounter(Base):
    """How much of one limit window a subject (an IP, a user, an email...)
    has used in the window's current period. One row per limit window and
    subject, reset in place when a new period starts (see
    `RateLimiter.consume`).

    `key` never holds the subject itself (it may be an IP or an email),
    only a hash of it. Rows whose period has ended are only history, and are
    pruned now and then (`expires_at`)."""

    __tablename__ = "rate_limit_counters"
    __table_args__ = (Index("ix_rate_limit_counters_expires_at", "expires_at"),)

    # "<limit name>:<window seconds>:<hash of the subject>".
    key: Mapped[str] = mapped_column(String, primary_key=True)
    # Start of the current period, in Unix seconds (a multiple of the
    # window's length).
    window_start: Mapped[int] = mapped_column(BigInteger)
    # Used so far in that period: requests, or e.g. bytes for byte quotas.
    count: Mapped[int] = mapped_column(BigInteger)
    # When that period ends.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
