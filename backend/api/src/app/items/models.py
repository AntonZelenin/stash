import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Table,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType
from stash_shared.embeddings import EMBEDDING_DIMENSIONS

from app.db import Base


class Vector(UserDefinedType):
    """pgvector's `vector(n)` column type. The API never reads or writes
    vectors through the ORM (search binds the query vector as text and casts
    it in SQL; the embedding worker writes rows), so no value conversion is
    needed — just the DDL/type."""

    cache_ok = True

    def __init__(self, dimensions: int):
        self.dimensions = dimensions

    def get_col_spec(self, **kw) -> str:
        return f"VECTOR({self.dimensions})"


class ItemType(str, Enum):
    text = "text"
    link = "link"
    image = "image"
    file = "file"


class ItemStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class Item(Base):
    __tablename__ = "items"
    # Serves every per-user lookup, and counting a user's items by type
    # (`ItemRepository.count_by_type`) from the index.
    __table_args__ = (Index("ix_items_user_id_type", "user_id", "type"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    type: Mapped[ItemType] = mapped_column(SqlEnum(ItemType, name="item_type"))
    status: Mapped[ItemStatus] = mapped_column(
        SqlEnum(ItemStatus, name="item_status"), default=ItemStatus.pending
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When `status` last changed — or, for `processing`, when the last
    # processing attempt started.
    status_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    text_content: Mapped["TextContent | None"] = relationship(back_populates="item", uselist=False)
    image: Mapped["ImageMetadata | None"] = relationship(back_populates="item", uselist=False)
    file: Mapped["FileMetadata | None"] = relationship(back_populates="item", uselist=False)
    description: Mapped["Description | None"] = relationship(back_populates="item", uselist=False)
    embedding: Mapped["Embedding | None"] = relationship(back_populates="item", uselist=False)
    # Sorted by name so every client shows them in the same order.
    tags: Mapped[list["Tag"]] = relationship(secondary="item_tags", order_by="Tag.name")


class TextContent(Base):
    __tablename__ = "item_text_contents"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    text: Mapped[str] = mapped_column(String)

    item: Mapped[Item] = relationship(back_populates="text_content")


class ImageMetadata(Base):
    __tablename__ = "item_images"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    storage_key: Mapped[str] = mapped_column(String)
    content_type: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    # Small WebP version for display, written by the thumbnail worker; None
    # until it has run (clients fall back to the original).
    thumbnail_key: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    item: Mapped[Item] = relationship(back_populates="image")


class FileMetadata(Base):
    __tablename__ = "item_files"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    storage_key: Mapped[str] = mapped_column(String)
    # As uploaded (path components stripped), for display and as the
    # download's filename.
    filename: Mapped[str] = mapped_column(String)
    # Derived from the validated file type, not the client's header.
    content_type: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)

    item: Mapped[Item] = relationship(back_populates="file")


class PendingUpload(Base):
    """An image or file upload that was started but not finalized yet.

    Started: the API picked the item id (this row's `id`) and storage key,
    stored the metadata the client declared, and handed out a pre-signed URL
    for that key. The client then uploads straight to storage and finalizes,
    which creates the item (same id) and deletes this row in one
    transaction. Every row left is therefore an unfinished upload: the
    object at `storage_key` may or may not exist, and no item points at it.
    Nothing cleans these up yet; a cleanup job would delete the object and
    the row once `expires_at` is well past (see the architecture doc)."""

    __tablename__ = "pending_uploads"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # `image` or `file`: the item this upload becomes.
    type: Mapped[ItemType] = mapped_column(SqlEnum(ItemType, name="item_type"))
    # Generated by the API, never sent by the client.
    storage_key: Mapped[str] = mapped_column(String)
    # What the upload URL was signed for; the content is still checked on
    # finalize.
    content_type: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    # Files only: the cleaned filename, for display.
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    # The new item's caption and tag names (already normalized).
    caption: Mapped[str | None] = mapped_column(String, nullable=True)
    tag_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When the upload URL stops being accepted.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Description(Base):
    __tablename__ = "item_descriptions"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    text: Mapped[str] = mapped_column(String)

    item: Mapped[Item] = relationship(back_populates="description")


# Many-to-many: which tags are assigned to which items. Rows go away with
# either side (ON DELETE CASCADE).
item_tags = Table(
    "item_tags",
    Base.metadata,
    Column("item_id", Uuid, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Uuid, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
    # When the tag was put on the item: tag suggestions rank by it. Also set
    # in Python, so links made within the same second still order correctly.
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    ),
    # The primary key covers lookups by item; this covers filtering by tag
    # and, with `created_at`, counting a tag's uses and finding its latest
    # one (see `TagRepository.used_tags`).
    Index("ix_item_tags_tag_id_created_at", "tag_id", "created_at"),
)


class Tag(Base):
    """A user's custom label for items. Each user has their own set: names
    are unique per user, case-insensitively ("Python" and "python" are the
    same tag), and one user never sees another's tags."""

    __tablename__ = "tags"
    # lower(name) as SQL text: `func.lower("name")` would lower the *string*
    # "name", indexing a constant.
    __table_args__ = (Index("uq_tags_user_id_lower_name", "user_id", text("lower(name)"), unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    # As first entered (e.g. "Python"); matched case-insensitively.
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Embedding(Base):
    __tablename__ = "item_embeddings"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    # Embedding of the item's description (see `stash_shared.embeddings`),
    # written by the embedding worker; searched by cosine distance.
    embedding: Mapped[str] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    # Hash of the exact description text `embedding` was made from.
    content_hash: Mapped[str] = mapped_column(String)

    item: Mapped[Item] = relationship(back_populates="embedding")
