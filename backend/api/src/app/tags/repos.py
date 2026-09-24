import uuid

from sqlalchemy import case, delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Item, Tag, item_tags


def _escape_like(value: str) -> str:
    """Makes user input match literally inside a LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TagRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_or_create_for_linking(self, *, user_id: uuid.UUID, names: list[str]) -> list[Tag]:
        """The user's tags with these (normalized, distinct) names, in the
        same order: existing ones reused, case-insensitively, and missing ones
        created. Every one is locked until the transaction ends, so it
        can be linked to an item in that transaction.

        The lock is `FOR KEY SHARE`, the same one Postgres takes when a
        link row is inserted. It conflicts with the `FOR UPDATE` that
        `delete_orphans` takes, so an unused tag can't be deleted between
        being found here and being linked. If `delete_orphans` got there
        first, this waits for it; a deleted tag is then not found, and is
        created again. Rows are locked in id order, as `delete_orphans`
        locks them, so the two can't deadlock.

        A concurrent request can create the same missing tag first. The
        unique index then rejects this one's insert (inside a savepoint,
        so the rest of the transaction is kept), and the other's tag is
        used instead.
        """
        existing = await self._lock_by_names(user_id=user_id, names=names)
        tags = []
        for name in names:
            tag = existing.get(name.lower())
            if tag is None:
                try:
                    async with self._session.begin_nested():
                        tag = Tag(user_id=user_id, name=name)
                        self._session.add(tag)
                except IntegrityError:
                    tag = (await self._lock_by_names(user_id=user_id, names=[name])).get(name.lower())
                    if tag is None:
                        raise
            tags.append(tag)
        return tags

    async def _lock_by_names(self, *, user_id: uuid.UUID, names: list[str]) -> dict[str, Tag]:
        """The user's tags with these names, keyed by lowercase name and
        locked `FOR KEY SHARE` in id order."""
        result = await self._session.execute(
            select(Tag)
            .where(Tag.user_id == user_id, func.lower(Tag.name).in_([name.lower() for name in names]))
            .order_by(Tag.id)
            .with_for_update(read=True, key_share=True)
        )
        return {tag.name.lower(): tag for tag in result.scalars()}

    async def delete_orphans(self, *, user_id: uuid.UUID, tag_ids: list[uuid.UUID]) -> None:
        """Deletes the ones among the user's tags `tag_ids` that no item
        uses any more. Call it in the transaction that removed their links,
        after removing them.

        Each tag is first locked `FOR UPDATE`. That waits for any
        transaction still linking one of them (see
        `get_or_create_for_linking`) to finish. The check for remaining links
        runs after the locks are held, and under READ COMMITTED it sees every
        link committed by then. A tag that is found unused therefore
        can't gain a link before this transaction commits: linking it
        would need the lock held here.
        """
        if not tag_ids:
            return
        locked = (
            await self._session.execute(
                select(Tag.id)
                .where(Tag.user_id == user_id, Tag.id.in_(set(tag_ids)))
                .order_by(Tag.id)
                .with_for_update()
            )
        ).scalars().all()
        if not locked:
            return
        in_use = set(
            (
                await self._session.execute(
                    select(item_tags.c.tag_id).where(item_tags.c.tag_id.in_(locked)).distinct()
                )
            ).scalars()
        )
        orphans = [tag_id for tag_id in locked if tag_id not in in_use]
        if orphans:
            await self._session.execute(delete(Tag).where(Tag.id.in_(orphans)))

    async def search(self, *, user_id: uuid.UUID, query: str, limit: int) -> list[Tag]:
        """The user's tags containing `query` (case-insensitive; all of them
        if it's empty): names starting with it first, then alphabetically."""
        stmt = select(Tag).where(Tag.user_id == user_id)
        order = [func.lower(Tag.name)]
        if query:
            needle = _escape_like(query.lower())
            stmt = stmt.where(func.lower(Tag.name).like(f"%{needle}%", escape="\\"))
            starts_with = func.lower(Tag.name).like(f"{needle}%", escape="\\")
            order.insert(0, case((starts_with, 0), else_=1))
        result = await self._session.execute(stmt.order_by(*order).limit(limit))
        return list(result.scalars().all())

    async def item_belongs_to_user(self, *, item_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        result = await self._session.execute(select(Item.id).where(Item.id == item_id, Item.user_id == user_id))
        return result.first() is not None

    async def is_assigned(self, *, item_id: uuid.UUID, tag_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(item_tags.c.item_id).where(item_tags.c.item_id == item_id, item_tags.c.tag_id == tag_id)
        )
        return result.first() is not None

    async def assign(self, *, item_id: uuid.UUID, tag_id: uuid.UUID) -> None:
        await self._session.execute(insert(item_tags).values(item_id=item_id, tag_id=tag_id))

    async def unassign(self, *, item_id: uuid.UUID, tag_id: uuid.UUID) -> bool:
        """Whether the tag was assigned (and now isn't)."""
        result = await self._session.execute(
            delete(item_tags).where(item_tags.c.item_id == item_id, item_tags.c.tag_id == tag_id)
        )
        return result.rowcount == 1
