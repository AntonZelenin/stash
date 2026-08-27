from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.repos import TokenRepository
from app.auth.security import generate_token, hash_token, verify_password
from app.config import get_settings
from app.users.models import User
from app.users.repos import UserRepository


class InvalidCredentialsError(Exception):
    pass


class InvalidRefreshTokenError(Exception):
    pass


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str


class AuthService:
    def __init__(self, session: AsyncSession):
        self._users = UserRepository(session)
        self._tokens = TokenRepository(session)

    async def authenticate(self, email: str, password: str) -> User:
        user = await self._users.get_by_email(email)
        if user is None or not verify_password(password, user.password_hash):
            raise InvalidCredentialsError()
        return user

    async def issue_tokens(self, user: User) -> TokenPair:
        settings = get_settings()
        now = datetime.now(timezone.utc)

        access_token = generate_token()
        await self._tokens.create_access_token(
            user_id=user.id,
            token_hash=hash_token(access_token),
            expires_at=now + timedelta(minutes=settings.access_token_ttl_minutes),
        )

        refresh_token = generate_token()
        await self._tokens.create_refresh_token(
            user_id=user.id,
            token_hash=hash_token(refresh_token),
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
        )

        return TokenPair(access_token=access_token, refresh_token=refresh_token)

    async def refresh(self, refresh_token: str) -> TokenPair:
        stored = await self._tokens.get_valid_refresh_token(hash_token(refresh_token))
        if stored is None:
            raise InvalidRefreshTokenError()

        # Rotate: this refresh token is single-use.
        await self._tokens.revoke_refresh_token(stored)

        user = await self._users.get_by_id(stored.user_id)
        if user is None:
            raise InvalidRefreshTokenError()

        return await self.issue_tokens(user)

    async def get_user_by_access_token(self, token: str) -> User | None:
        stored = await self._tokens.get_valid_access_token(hash_token(token))
        if stored is None:
            return None
        return await self._users.get_by_id(stored.user_id)
