from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.services import AuthService
from app.db import get_db_session
from app.users.models import User

_bearer_scheme = HTTPBearer(scheme_name="bearerAuth", auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    if credentials is None:
        raise _unauthorized()

    user = await AuthService(session).get_user_by_access_token(credentials.credentials)
    if user is None:
        raise _unauthorized()

    return user
