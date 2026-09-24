from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.log import get_logger

from app.auth.repos import TokenRepository
from app.auth.security import generate_token, hash_password, hash_token, verify_password
from app.config import get_settings
from app.users.models import User
from app.users.repos import UserRepository

logger = get_logger(__name__)


class InvalidCredentialsError(Exception):
    pass


class InvalidRefreshTokenError(Exception):
    pass


class IncorrectPasswordError(Exception):
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
        # Emails are personal data and never logged; the user id identifies.
        user = await self._users.get_by_email(email)
        if user is None:
            logger.warning("Login failed", reason="unknown_email")
            raise InvalidCredentialsError()
        if not verify_password(password, user.password_hash):
            logger.warning("Login failed", reason="wrong_password", user_id=user.id)
            raise InvalidCredentialsError()
        logger.info("Login succeeded", user_id=user.id)
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
            logger.warning("Token refresh rejected", reason="invalid_or_expired_token")
            raise InvalidRefreshTokenError()

        # Rotate: this refresh token is single-use.
        await self._tokens.revoke_refresh_token(stored)

        user = await self._users.get_by_id(stored.user_id)
        if user is None:
            logger.warning("Token refresh rejected", reason="user_not_found", user_id=stored.user_id)
            raise InvalidRefreshTokenError()

        logger.info("Tokens refreshed", user_id=user.id)
        return await self.issue_tokens(user)

    async def change_password(self, user: User, current_password: str, new_password: str) -> TokenPair:
        """Replaces the user's password after checking the current one.

        Every existing session is ended, so anyone holding the user's old
        tokens is signed out; the returned pair keeps the caller signed in.
        """
        if not verify_password(current_password, user.password_hash):
            logger.warning("Password change rejected", reason="wrong_current_password", user_id=user.id)
            raise IncorrectPasswordError()

        user.password_hash = hash_password(new_password)
        await self._tokens.revoke_all_for_user(user.id)
        logger.info("Password changed; all sessions revoked", user_id=user.id)
        return await self.issue_tokens(user)

    async def get_user_by_access_token(self, token: str) -> User | None:
        stored = await self._tokens.get_valid_access_token(hash_token(token))
        if stored is None:
            return None
        return await self._users.get_by_id(stored.user_id)
