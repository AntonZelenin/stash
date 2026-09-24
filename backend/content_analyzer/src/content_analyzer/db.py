from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from stash_shared import tracing

from content_analyzer.config import get_settings


def create_engine() -> AsyncEngine:
    engine = create_async_engine(get_settings().database_url)
    tracing.instrument_sqlalchemy(engine)
    return engine
