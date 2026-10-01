import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.log import get_logger

from app.analytics import Analytics, DisabledAnalytics, Event
from app.items.models import Tag
from app.items.services import ItemNotFoundError
from app.tags.names import InvalidTagNameError, normalize_tag_name
from app.tags.repos import TagRepository, TagUsage
from app.users.models import User

__all__ = ["InvalidTagNameError", "MAX_HIDDEN_TAGS", "TagService", "TooManyHiddenTagsError"]

logger = get_logger(__name__)

# Most tags a user can hide: as many as one request can exclude (the API's
# `MAX_EXCLUDED_TAGS`), since the clients' Blind mode sends them all.
MAX_HIDDEN_TAGS = 100


class TooManyHiddenTagsError(Exception):
    pass


def tag_visibility(tag: Tag) -> str:
    """A tag's `tag_visibility` in analytics (`item_tags_changed`)."""
    return "hidden" if tag.is_hidden else "regular"


class TagService:
    def __init__(self, session: AsyncSession, analytics: Analytics | None = None):
        self._session = session
        self._repo = TagRepository(session)
        self._analytics = analytics or DisabledAnalytics()

    async def search_tags(
        self, *, user_id: uuid.UUID, query: str, limit: int, excluded_tag_ids: tuple[uuid.UUID, ...] = ()
    ) -> list[TagUsage]:
        return await self._repo.search(
            user_id=user_id, query=" ".join(query.split()), limit=limit, excluded_tag_ids=excluded_tag_ids
        )

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

    async def assign_tag(self, *, user: User, item_id: uuid.UUID, name: str) -> Tag:
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
                tag, assigned = await self._assign(user_id=user.id, item_id=item_id, name=name)
                if assigned:
                    self._analytics.capture(
                        user.analytics_id,
                        Event.item_tags_changed,
                        {"action": "add", "tag_visibility": tag_visibility(tag), "affected_item_count": 1},
                    )
                return tag
            except IntegrityError:
                await self._session.rollback()
                if attempt:
                    raise
                # A concurrent request linked it first (or the item was
                # just deleted); the retry sees which.
                logger.warning("Tag assignment conflicted; retrying", item_id=item_id, attempt=attempt + 1)
        raise AssertionError("unreachable")

    async def _assign(self, *, user_id: uuid.UUID, item_id: uuid.UUID, name: str) -> tuple[Tag, bool]:
        """The tag, and whether it's newly assigned (False: it already was)."""
        if not await self._repo.item_belongs_to_user(item_id=item_id, user_id=user_id):
            raise ItemNotFoundError()
        [tag] = await self._repo.get_or_create_for_linking(user_id=user_id, names=[name])
        assigned = not await self._repo.is_assigned(item_id=item_id, tag_id=tag.id)
        if assigned:
            await self._repo.assign(item_id=item_id, tag_id=tag.id)
        await self._session.commit()
        return tag, assigned

    async def remove_tag(self, *, user: User, item_id: uuid.UUID, tag_id: uuid.UUID) -> None:
        """Removes the tag from the user's item (a no-op if it wasn't
        assigned). If no other item uses the tag, it's deleted too, in the
        same transaction."""
        if not await self._repo.item_belongs_to_user(item_id=item_id, user_id=user.id):
            raise ItemNotFoundError()
        # Read before the link goes: the tag may go with it.
        tag = await self._repo.get(user_id=user.id, tag_id=tag_id)
        visibility = tag_visibility(tag) if tag is not None else "regular"
        removed = await self._repo.unassign(item_id=item_id, tag_id=tag_id)
        if removed:
            await self._repo.delete_orphans(user_id=user.id, tag_ids=[tag_id])
        await self._session.commit()
        if removed:
            self._analytics.capture(
                user.analytics_id,
                Event.item_tags_changed,
                {"action": "remove", "tag_visibility": visibility, "affected_item_count": 1},
            )

    async def hidden_tags(self, *, user_id: uuid.UUID) -> list[Tag]:
        """The user's hidden tags, by name."""
        return await self._repo.hidden(user_id=user_id)

    async def set_hidden(
        self, *, user: User, tag_ids: list[uuid.UUID], hidden: bool, imported_from_device: bool = False
    ) -> int:
        """Hides (or shows again) these of the user's tags; ids that aren't
        the user's tags (another's, or one since deleted) are skipped.
        Returns how many changed. At most `MAX_HIDDEN_TAGS` can be hidden
        (`TooManyHiddenTagsError`, nothing changed).

        `imported_from_device`: a client uploading the hidden tags it used
        to keep on the device. Applied the same, but it isn't something the
        user did now, so it isn't counted in analytics."""
        changed = await self._repo.set_hidden(user_id=user.id, tag_ids=sorted(set(tag_ids)), hidden=hidden)
        if hidden and changed and await self._repo.count_hidden(user_id=user.id) > MAX_HIDDEN_TAGS:
            await self._session.rollback()
            raise TooManyHiddenTagsError()
        await self._session.commit()
        logger.info("Tag visibility changed", hidden=hidden, tag_count=changed, imported=imported_from_device)
        if changed and not imported_from_device:
            self._analytics.capture(
                user.analytics_id,
                Event.tag_visibility_changed,
                {"visibility": "hidden" if hidden else "visible", "affected_tag_count": changed},
            )
        return changed
