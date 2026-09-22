from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from content_analyzer.config import get_settings


def create_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url)
