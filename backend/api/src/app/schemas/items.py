from enum import Enum
from uuid import UUID

from pydantic import BaseModel


class ItemType(str, Enum):
    text = "text"
    image = "image"


class ItemStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class CreateTextItemRequest(BaseModel):
    text: str


class ItemCreated(BaseModel):
    id: UUID
    status: ItemStatus
