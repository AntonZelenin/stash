from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from stash_shared import tracing


def create_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url)
    tracing.instrument_sqlalchemy(engine)
    return engine
