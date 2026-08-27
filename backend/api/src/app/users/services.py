from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_password
from app.users.models import User
from app.users.repos import UserRepository


class EmailAlreadyRegisteredError(Exception):
    pass


class UserService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._repo = UserRepository(session)

    async def register(self, email: str, password: str) -> User:
        try:
            return await self._repo.create(email=email, password_hash=hash_password(password))
        except IntegrityError:
            await self._session.rollback()
            raise EmailAlreadyRegisteredError(email) from None
