import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decode_access_token
from app.db import get_db_session
from app.users.models import User
from app.users.repos import UserRepository

_bearer_scheme = HTTPBearer(scheme_name="bearerAuth", bearerFormat="JWT", auto_error=False)


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

    try:
        user_id = decode_access_token(credentials.credentials)
    except (jwt.PyJWTError, ValueError):
        raise _unauthorized() from None

    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        raise _unauthorized()

    return user
