from collections.abc import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(get_settings().database_url)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Inject the session with this, never `Depends(get_db_session)` directly.
#
# `scope="function"` makes FastAPI run `get_db_session`'s commit as soon as
# the endpoint returns, *before* the response is sent. By default it runs
# after, so a client acting on a response immediately — using the token a
# login just returned, listing right after a save — could race the commit
# and not see its own write yet.
#
# Every injection site must use this same object: FastAPI shares one session
# per request between e.g. `get_current_user` and the endpoint only when the
# dependency (scope included) is identical.
DbSession = Depends(get_db_session, scope="function")
