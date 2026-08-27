import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Uuid, func
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ItemType(str, Enum):
    text = "text"
    image = "image"


class ItemStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class Item(Base):
    __tablename__ = "items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    type: Mapped[ItemType] = mapped_column(SqlEnum(ItemType, name="item_type"))
    status: Mapped[ItemStatus] = mapped_column(
        SqlEnum(ItemStatus, name="item_status"), default=ItemStatus.pending
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    text_content: Mapped["TextContent | None"] = relationship(back_populates="item", uselist=False)
    image: Mapped["ImageMetadata | None"] = relationship(back_populates="item", uselist=False)
    description: Mapped["Description | None"] = relationship(back_populates="item", uselist=False)
    embedding: Mapped["Embedding | None"] = relationship(back_populates="item", uselist=False)
    tags: Mapped[list["ItemTag"]] = relationship(back_populates="item")


class TextContent(Base):
    __tablename__ = "item_text_contents"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id"), primary_key=True)
    text: Mapped[str] = mapped_column(String)

    item: Mapped[Item] = relationship(back_populates="text_content")


class ImageMetadata(Base):
    __tablename__ = "item_images"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id"), primary_key=True)
    storage_key: Mapped[str] = mapped_column(String)
    content_type: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)

    item: Mapped[Item] = relationship(back_populates="image")


class Description(Base):
    __tablename__ = "item_descriptions"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id"), primary_key=True)
    text: Mapped[str] = mapped_column(String)

    item: Mapped[Item] = relationship(back_populates="description")


class ItemTag(Base):
    __tablename__ = "item_tags"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id"), primary_key=True)
    tag: Mapped[str] = mapped_column(String, primary_key=True)

    item: Mapped[Item] = relationship(back_populates="tags")


class Embedding(Base):
    __tablename__ = "item_embeddings"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id"), primary_key=True)
    vector: Mapped[list[float]] = mapped_column(ARRAY(Float))

    item: Mapped[Item] = relationship(back_populates="embedding")
