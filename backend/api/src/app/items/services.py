import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Item
from app.items.repos import ItemRepository


class ItemService:
    def __init__(self, session: AsyncSession):
        self._repo = ItemRepository(session)

    async def create_text_item(self, *, user_id: uuid.UUID, text: str) -> Item:
        return await self._repo.create_text_item(user_id=user_id, text=text)
