import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    JSON,
    String,
    Table,
    UniqueConstraint,
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
    # Serves every per-user lookup, and finding a user's items by type
    # (`ItemRepository.count`, the type filter).
    __table_args__ = (Index("ix_items_user_id_type", "user_id", "type"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
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
    # Deleted by the database with the item (never loaded to delete them).
    search_chunks: Mapped[list["SearchChunk"]] = relationship(
        back_populates="item", order_by="SearchChunk.position", passive_deletes=True
    )
    # Sorted by name so every client shows them in the same order.
    tags: Mapped[list["Tag"]] = relationship(secondary="item_tags", order_by="Tag.name")
    # Likewise sorted by name.
    collections: Mapped[list["Collection"]] = relationship(
        secondary="item_collections", order_by="Collection.name"
    )


class TextContent(Base):
    __tablename__ = "item_text_contents"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    text: Mapped[str] = mapped_column(String)

    item: Mapped[Item] = relationship(back_populates="text_content")


class ImageMetadata(Base):
    __tablename__ = "item_images"
    # Finding earlier uploads with the same name and size before an upload
    # (`ItemRepository.duplicate_groups`).
    # Keys: looking up whether a stored object is referenced
    # (`app.storage.reconciliation`).
    __table_args__ = (
        Index("ix_item_images_filename_size_bytes", "filename", "size_bytes"),
        Index("ix_item_images_storage_key", "storage_key"),
        Index("ix_item_images_thumbnail_key", "thumbnail_key"),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    # The canonical, write-once copy of the validated upload; never a
    # staging key (see `stash_shared.storage_keys`).
    storage_key: Mapped[str] = mapped_column(String)
    # That object's ETag: a change-detection token (workers read the
    # object only while it has it), not a content hash. None for legacy
    # items (finalized in place, before canonical copies existed).
    content_etag: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Hex SHA-256 of that object's content, computed by the storage as it
    # wrote the copy: the content's identity. None for legacy items.
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    content_type: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    # As uploaded (path components stripped), used as the download's
    # filename. None for images uploaded before it was kept.
    filename: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Small WebP version for display, written by the thumbnail worker; None
    # until it has run (clients fall back to the original).
    thumbnail_key: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    item: Mapped[Item] = relationship(back_populates="image")


class FileMetadata(Base):
    __tablename__ = "item_files"
    # Like `ImageMetadata`'s.
    __table_args__ = (
        Index("ix_item_files_filename_size_bytes", "filename", "size_bytes"),
        Index("ix_item_files_storage_key", "storage_key"),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    # Canonical and write-once, like `ImageMetadata.storage_key`.
    storage_key: Mapped[str] = mapped_column(String)
    # Like `ImageMetadata.content_etag` and `content_sha256`.
    content_etag: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
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
    which copies the validated content to a canonical key, creates the item
    (same id, canonical key) and deletes this row in one transaction. Every
    row left is therefore an unfinished upload: the object at `storage_key`
    may or may not exist, and no item points at it. Rows abandoned for
    longer than `ABANDONED_UPLOAD_GRACE` are deleted (see
    `ItemService.start_upload`), and their staging objects expire by the
    bucket's lifecycle rule."""

    __tablename__ = "pending_uploads"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # `image` or `file`: the item this upload becomes.
    type: Mapped[ItemType] = mapped_column(SqlEnum(ItemType, name="item_type"))
    # The staging key the upload URL is signed for (`uploads/...`; the
    # upload's own key for rows started before staging keys existed).
    # Generated by the API, never sent by the client.
    storage_key: Mapped[str] = mapped_column(String)
    # What the upload URL was signed for; the content is still checked on
    # finalize.
    content_type: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    # The cleaned filename, for display and downloads (images: None if the
    # client sent none).
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    # The new item's caption, and tag and collection names (already
    # normalized).
    caption: Mapped[str | None] = mapped_column(String, nullable=True)
    tag_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    collection_names: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
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
    # Hidden by the user: the clients' Blind mode leaves out items carrying
    # it, on every device. Gone with the tag.
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


# Many-to-many: which items are in which collections. Rows go away with
# either side (ON DELETE CASCADE).
item_collections = Table(
    "item_collections",
    Base.metadata,
    Column("item_id", Uuid, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True),
    Column("collection_id", Uuid, ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True),
    # When the item was added to the collection.
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    ),
    # The primary key covers lookups by item; this covers filtering by
    # collection.
    Index("ix_item_collections_collection_id", "collection_id"),
)


class Collection(Base):
    """A user's named group of items. Like tags, private to their owner,
    unique per user case-insensitively, an item can be in any number of
    them, and one exists only while some item is in it (see
    `CollectionRepository`)."""

    __tablename__ = "collections"
    __table_args__ = (
        Index("uq_collections_user_id_lower_name", "user_id", text("lower(name)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    # As first entered; matched case-insensitively.
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SearchChunk(Base):
    """One short piece of an item's searchable text, embedded on its own:
    a note's or link's text, a caption, one of an image's generated search
    chunks (see `stash_shared.descriptions.search_chunks`). An item matches
    a semantic search by its best-matching chunk, so a short query ("girl")
    isn't diluted by everything else the image shows.

    Written only by the embedding worker, which replaces all of an item's
    chunks at once whenever its searchable text changes; they go with the
    item (ON DELETE CASCADE)."""

    __tablename__ = "item_search_chunks"
    # Leads with `item_id`: serves the per-item replace and cascade, and
    # keeps each item's chunks in order.
    __table_args__ = (UniqueConstraint("item_id", "position", name="uq_item_search_chunks_item_id_position"),)

    # INTEGER on SQLite (tests), where only that auto-increments.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), Identity(always=True), primary_key=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("items.id", ondelete="CASCADE"))
    # The chunk's order within the item's text.
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(String)
    # Embedding of `text` (see `stash_shared.embeddings`); searched by
    # cosine distance.
    embedding: Mapped[str] = mapped_column(Vector(EMBEDDING_DIMENSIONS))

    item: Mapped[Item] = relationship(back_populates="search_chunks")
