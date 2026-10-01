import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Who the user is in product analytics (`app.analytics`), on the API
    # and every client alike: random, kept only for that, never derived
    # from the id or email, so an analytics event says nothing about which
    # account it is.
    analytics_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, default=uuid.uuid4)
    # The UI language the user chose (a code such as "en"), on every device
    # they sign in on; None until they choose one.
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
