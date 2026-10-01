from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared import storage_keys
from stash_shared.log import get_logger

from app.analytics import Analytics, DisabledAnalytics, Event
from app.auth.security import hash_password, verify_password
from app.auth.services import IncorrectPasswordError
from app.items.repos import ItemRepository
from app.storage.deletions import StorageDeletionDrainer, schedule_key_deletion, schedule_prefix_deletion
from app.users.models import User
from app.users.repos import UserRepository

logger = get_logger(__name__)


class EmailAlreadyRegisteredError(Exception):
    pass


# The UI languages an account can choose (the clients' translations).
SUPPORTED_LANGUAGES = frozenset({"en", "uk"})


class UnsupportedLanguageError(Exception):
    pass


class UserService:
    def __init__(
        self,
        session: AsyncSession,
        storage_deletions: StorageDeletionDrainer | None = None,
        analytics: Analytics | None = None,
    ):
        self._session = session
        self._repo = UserRepository(session)
        self._storage_deletions = storage_deletions
        self._analytics = analytics or DisabledAnalytics()

    async def register(self, email: str, password: str) -> User:
        try:
            user = await self._repo.create(email=email, password_hash=hash_password(password))
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            logger.info("Registration rejected", reason="email_already_registered")
            raise EmailAlreadyRegisteredError(email) from None
        logger.info("User registered", user_id=user.id)
        self._analytics.capture(user.analytics_id, Event.account_registered)
        return user

    async def set_language(self, user: User, language: str) -> None:
        """Saves the UI language the user chose, for every device they use.
        Choosing the current one again changes nothing."""
        if language not in SUPPORTED_LANGUAGES:
            raise UnsupportedLanguageError(language)
        if user.language == language:
            return
        user.language = language
        await self._session.commit()
        logger.info("Language changed", language=language)
        self._analytics.capture(user.analytics_id, Event.language_changed, {"language": language})

    async def delete_account(self, user: User, password: str) -> None:
        """Deletes the user and everything they own, after checking their
        password.

        In the database, one transaction: the user row, and with it, by
        `ON DELETE CASCADE`, their items (and every row derived from them),
        tags, collections, pending uploads and sessions, so every token
        stops working. Their stored objects (originals, thumbnails, staged
        uploads) are deleted from storage in the same transaction's wake:
        the deletion is recorded with the rows' (`app.storage.deletions`)
        and carried out after the commit, then retried by later drains
        until it succeeds, so storage being down never leaves objects
        behind for good, nor the database half deleted."""
        assert self._storage_deletions is not None, "deleting an account needs the storage deletion drainer"
        if not verify_password(password, user.password_hash):
            logger.warning("Account deletion rejected", reason="wrong_password", user_id=user.id)
            raise IncorrectPasswordError()

        prefixes = storage_keys.user_prefixes(user.id)
        deletion_ids = [await schedule_prefix_deletion(self._session, prefix) for prefix in prefixes]
        # Items stored before keys were scoped by user aren't under their
        # prefixes.
        legacy_keys = await ItemRepository(self._session).storage_keys_outside(user_id=user.id, prefixes=prefixes)
        deletion_ids += [await schedule_key_deletion(self._session, key) for key in legacy_keys]
        await self._session.execute(delete(User).where(User.id == user.id))
        await self._session.commit()
        logger.info("Account deleted", user_id=user.id, legacy_storage_key_count=len(legacy_keys))
        # The analytics id outlives the row only in this event: PostHog's
        # person for it is the deleted account's.
        self._analytics.capture(user.analytics_id, Event.account_deleted)

        await self._storage_deletions.drain(only=deletion_ids)
