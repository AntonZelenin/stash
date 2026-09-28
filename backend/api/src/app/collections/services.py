import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.log import get_logger

from app.collections.names import InvalidCollectionNameError, normalize_collection_name
from app.collections.repos import CollectionRepository
from app.items.models import Collection
from app.items.services import ItemNotFoundError

__all__ = ["CollectionService", "InvalidCollectionNameError"]

logger = get_logger(__name__)


class CollectionService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._repo = CollectionRepository(session)

    async def search_collections(self, *, user_id: uuid.UUID, query: str, limit: int) -> list[Collection]:
        return await self._repo.search(user_id=user_id, query=" ".join(query.split()), limit=limit)

    async def add_to_collection(self, *, user_id: uuid.UUID, item_id: uuid.UUID, name: str) -> Collection:
        """Puts the user's item in the collection called `name`, reusing the
        user's existing collection of that name (ignoring case) or creating
        it. A no-op if the item is already in it. Returns the collection.

        Two concurrent requests can both add the item to the same
        collection: the primary key lets only one win, and the other
        retries once, now finding it there. The retry also covers an item
        deleted between the ownership check and the link.
        """
        name = normalize_collection_name(name)
        for attempt in range(2):
            try:
                return await self._add(user_id=user_id, item_id=item_id, name=name)
            except IntegrityError:
                await self._session.rollback()
                if attempt:
                    raise
                logger.warning("Adding to a collection conflicted; retrying", item_id=item_id, attempt=attempt + 1)
        raise AssertionError("unreachable")

    async def _add(self, *, user_id: uuid.UUID, item_id: uuid.UUID, name: str) -> Collection:
        if not await self._repo.item_belongs_to_user(item_id=item_id, user_id=user_id):
            raise ItemNotFoundError()
        # Locked until the link is committed (see
        # `get_or_create_for_linking`).
        [collection] = await self._repo.get_or_create_for_linking(user_id=user_id, names=[name])
        if not await self._repo.contains(collection_id=collection.id, item_id=item_id):
            await self._repo.add_item(collection_id=collection.id, item_id=item_id)
        await self._session.commit()
        return collection

    async def remove_from_collection(
        self, *, user_id: uuid.UUID, item_id: uuid.UUID, collection_id: uuid.UUID
    ) -> None:
        """Takes the user's item out of the collection (a no-op if it wasn't
        in it). If no other item is in it, the collection is deleted too, in
        the same transaction."""
        if not await self._repo.item_belongs_to_user(item_id=item_id, user_id=user_id):
            raise ItemNotFoundError()
        if await self._repo.remove_item(collection_id=collection_id, item_id=item_id):
            await self._repo.delete_orphans(user_id=user_id, collection_ids=[collection_id])
        await self._session.commit()
