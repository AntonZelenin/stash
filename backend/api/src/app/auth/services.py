from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import verify_password
from app.users.models import User
from app.users.repos import UserRepository


class InvalidCredentialsError(Exception):
    pass


class AuthService:
    def __init__(self, session: AsyncSession):
        self._repo = UserRepository(session)

    async def authenticate(self, email: str, password: str) -> User:
        user = await self._repo.get_by_email(email)
        if user is None or not verify_password(password, user.password_hash):
            raise InvalidCredentialsError()
        return user
