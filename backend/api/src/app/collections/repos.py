import uuid

from sqlalchemy import case, delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Collection, Item, item_collections
from app.tags.repos import escape_like


class CollectionRepository:
    """Collections exist only while some item is in them, so this follows
    the same locking scheme as `TagRepository`: linking locks a collection
    `FOR KEY SHARE`, orphan cleanup locks it `FOR UPDATE` before checking
    for remaining items, both in id order."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_or_create_for_linking(self, *, user_id: uuid.UUID, names: list[str]) -> list[Collection]:
        """The user's collections with these (normalized, distinct) names,
        in the same order: existing ones reused, case-insensitively, and
        missing ones created. Every one is locked until the transaction
        ends, so it can be linked to an item in that transaction without
        `delete_orphans` deleting it first (see
        `TagRepository.get_or_create_for_linking`, which this mirrors).

        A concurrent request can create the same missing collection first:
        the unique index then rejects this one's insert (inside a savepoint,
        so the rest of the transaction is kept), and the other's is used
        instead.
        """
        existing = await self._lock_by_names(user_id=user_id, names=names)
        collections = []
        for name in names:
            collection = existing.get(name.lower())
            if collection is None:
                try:
                    async with self._session.begin_nested():
                        collection = Collection(user_id=user_id, name=name)
                        self._session.add(collection)
                except IntegrityError:
                    collection = (await self._lock_by_names(user_id=user_id, names=[name])).get(name.lower())
                    if collection is None:
                        raise
            collections.append(collection)
        return collections

    async def _lock_by_names(self, *, user_id: uuid.UUID, names: list[str]) -> dict[str, Collection]:
        """The user's collections with these names, keyed by lowercase name
        and locked `FOR KEY SHARE` in id order."""
        result = await self._session.execute(
            select(Collection)
            .where(Collection.user_id == user_id, func.lower(Collection.name).in_([name.lower() for name in names]))
            .order_by(Collection.id)
            .with_for_update(read=True, key_share=True)
        )
        return {collection.name.lower(): collection for collection in result.scalars()}

    async def delete_orphans(self, *, user_id: uuid.UUID, collection_ids: list[uuid.UUID]) -> None:
        """Deletes the ones among the user's collections `collection_ids`
        that no item is in any more. Call it in the transaction that removed
        their links, after removing them. Each is locked `FOR UPDATE` before
        the check, so none can gain an item before this commits (see
        `TagRepository.delete_orphans`, which this mirrors)."""
        if not collection_ids:
            return
        locked = (
            await self._session.execute(
                select(Collection.id)
                .where(Collection.user_id == user_id, Collection.id.in_(set(collection_ids)))
                .order_by(Collection.id)
                .with_for_update()
            )
        ).scalars().all()
        if not locked:
            return
        in_use = set(
            (
                await self._session.execute(
                    select(item_collections.c.collection_id)
                    .where(item_collections.c.collection_id.in_(locked))
                    .distinct()
                )
            ).scalars()
        )
        orphans = [collection_id for collection_id in locked if collection_id not in in_use]
        if orphans:
            await self._session.execute(delete(Collection).where(Collection.id.in_(orphans)))

    async def search(self, *, user_id: uuid.UUID, query: str, limit: int) -> list[Collection]:
        """The user's collections containing `query` (case-insensitive; all
        of them if it's empty): names starting with it first, then
        alphabetically."""
        stmt = select(Collection).where(Collection.user_id == user_id)
        order = [func.lower(Collection.name)]
        if query:
            needle = escape_like(query.lower())
            stmt = stmt.where(func.lower(Collection.name).like(f"%{needle}%", escape="\\"))
            starts_with = func.lower(Collection.name).like(f"{needle}%", escape="\\")
            order.insert(0, case((starts_with, 0), else_=1))
        result = await self._session.execute(stmt.order_by(*order, Collection.id).limit(limit))
        return list(result.scalars().all())

    async def item_belongs_to_user(self, *, item_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        result = await self._session.execute(select(Item.id).where(Item.id == item_id, Item.user_id == user_id))
        return result.first() is not None

    async def contains(self, *, collection_id: uuid.UUID, item_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(item_collections.c.item_id).where(
                item_collections.c.item_id == item_id, item_collections.c.collection_id == collection_id
            )
        )
        return result.first() is not None

    async def add_item(self, *, collection_id: uuid.UUID, item_id: uuid.UUID) -> None:
        await self._session.execute(insert(item_collections).values(item_id=item_id, collection_id=collection_id))

    async def remove_item(self, *, collection_id: uuid.UUID, item_id: uuid.UUID) -> bool:
        """Whether the item was in the collection (and now isn't)."""
        result = await self._session.execute(
            delete(item_collections).where(
                item_collections.c.item_id == item_id, item_collections.c.collection_id == collection_id
            )
        )
        return result.rowcount == 1
