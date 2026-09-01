import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Item, ItemType, TextContent


class ItemRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_text_item(self, *, user_id: uuid.UUID, text: str) -> Item:
        item = Item(user_id=user_id, type=ItemType.text, text_content=TextContent(text=text))
        self._session.add(item)
        await self._session.flush()
        return item
