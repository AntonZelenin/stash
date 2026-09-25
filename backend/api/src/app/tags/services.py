import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.log import get_logger

from app.items.models import Tag
from app.items.services import ItemNotFoundError
from app.tags.names import InvalidTagNameError, normalize_tag_name
from app.tags.repos import TagRepository

__all__ = ["InvalidTagNameError", "TagService"]

logger = get_logger(__name__)


class TagService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._repo = TagRepository(session)

    async def search_tags(self, *, user_id: uuid.UUID, query: str, limit: int) -> list[Tag]:
        return await self._repo.search(user_id=user_id, query=" ".join(query.split()), limit=limit)

    async def suggest_tags(self, *, user_id: uuid.UUID, item_id: uuid.UUID | None, limit: int) -> list[Tag]:
        """Up to `limit` of the user's tags to offer when adding one: the
        most recently used first, then the most frequently used, without
        duplicates. Recent ones fill at most half the list (rounded up) so
        frequent ones always get a place, unless there aren't enough of
        them. With `item_id`, tags already on that item are left out."""
        if item_id is not None and not await self._repo.item_belongs_to_user(item_id=item_id, user_id=user_id):
            raise ItemNotFoundError()
        recent = await self._repo.used_tags(user_id=user_id, by="recent", exclude_item_id=item_id, limit=limit)
        frequent = await self._repo.used_tags(user_id=user_id, by="frequent", exclude_item_id=item_id, limit=limit)
        recent_share = (limit + 1) // 2
        suggestions: dict[uuid.UUID, Tag] = {}
        for tag in [*recent[:recent_share], *frequent, *recent[recent_share:]]:
            suggestions.setdefault(tag.id, tag)
        return list(suggestions.values())[:limit]

    async def assign_tag(self, *, user_id: uuid.UUID, item_id: uuid.UUID, name: str) -> Tag:
        """Assigns the tag called `name` to the user's item, reusing the
        user's existing tag of that name (ignoring case) or creating it.
        Assigning an already-assigned tag is a no-op. Returns the tag.

        The tag is locked until the link is committed, so orphan cleanup
        can't delete it in between (see `get_or_create_for_linking`). Two
        concurrent requests can both link the same tag to the same item:
        the primary key lets only one win, and the other retries once, now
        finding it assigned. The retry also covers an item deleted between
        the ownership check and the link.
        """
        name = normalize_tag_name(name)
        for attempt in range(2):
            try:
                return await self._assign(user_id=user_id, item_id=item_id, name=name)
            except IntegrityError:
                await self._session.rollback()
                if attempt:
                    raise
                # A concurrent request linked it first (or the item was
                # just deleted); the retry sees which.
                logger.warning("Tag assignment conflicted; retrying", item_id=item_id, attempt=attempt + 1)
        raise AssertionError("unreachable")

    async def _assign(self, *, user_id: uuid.UUID, item_id: uuid.UUID, name: str) -> Tag:
        if not await self._repo.item_belongs_to_user(item_id=item_id, user_id=user_id):
            raise ItemNotFoundError()
        [tag] = await self._repo.get_or_create_for_linking(user_id=user_id, names=[name])
        if not await self._repo.is_assigned(item_id=item_id, tag_id=tag.id):
            await self._repo.assign(item_id=item_id, tag_id=tag.id)
        await self._session.commit()
        return tag

    async def remove_tag(self, *, user_id: uuid.UUID, item_id: uuid.UUID, tag_id: uuid.UUID) -> None:
        """Removes the tag from the user's item (a no-op if it wasn't
        assigned). If no other item uses the tag, it's deleted too, in the
        same transaction."""
        if not await self._repo.item_belongs_to_user(item_id=item_id, user_id=user_id):
            raise ItemNotFoundError()
        if await self._repo.unassign(item_id=item_id, tag_id=tag_id):
            await self._repo.delete_orphans(user_id=user_id, tag_ids=[tag_id])
        await self._session.commit()
