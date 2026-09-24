import uuid

from sqlalchemy import case, delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Item, Tag, item_tags


def _escape_like(value: str) -> str:
    """Makes user input match literally inside a LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TagRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def find_by_name(self, *, user_id: uuid.UUID, name: str) -> Tag | None:
        result = await self._session.execute(
            select(Tag).where(Tag.user_id == user_id, func.lower(Tag.name) == name.lower())
        )
        return result.scalar_one_or_none()

    async def create(self, *, user_id: uuid.UUID, name: str) -> Tag:
        tag = Tag(user_id=user_id, name=name)
        self._session.add(tag)
        await self._session.flush()
        return tag

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

    async def unassign(self, *, item_id: uuid.UUID, tag_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(item_tags).where(item_tags.c.item_id == item_id, item_tags.c.tag_id == tag_id)
        )
