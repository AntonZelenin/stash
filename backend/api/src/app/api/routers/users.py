from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.auth import TokenPairResponse
from app.api.schemas.users import ChangePasswordRequest, UserCreateRequest, UserCreateResponse
from app.auth.services import AuthService, IncorrectPasswordError
from app.db import DbSession
from app.dependencies import get_current_user
from app.users.models import User
from app.users.services import EmailAlreadyRegisteredError, UserService

router = APIRouter(tags=["users"])


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    response_model=UserCreateResponse,
    responses={
        409: {"description": "User already exists"},
        422: {"description": "Invalid request"},
    },
)
async def create_user(
    payload: UserCreateRequest,
    session: AsyncSession = DbSession,
) -> UserCreateResponse:
    try:
        user = await UserService(session).register(payload.email, payload.password)
    except EmailAlreadyRegisteredError:
        raise HTTPException(status.HTTP_409_CONFLICT, "User already exists") from None

    return UserCreateResponse(id=user.id)


@router.post(
    "/users/me/password",
    status_code=status.HTTP_200_OK,
    response_model=TokenPairResponse,
    responses={
        401: {"description": "Unauthorized"},
        422: {"description": "Current password is incorrect, or the new one is invalid"},
    },
)
async def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
) -> TokenPairResponse:
    try:
        tokens = await AuthService(session).change_password(
            current_user, payload.current_password, payload.new_password
        )
    except IncorrectPasswordError:
        # Same shape as a request validation error, so clients can show the
        # message on the current-password field. Deliberately not a 401: the
        # caller's token is fine, and clients treat 401 as "sign in again".
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"loc": ["body", "current_password"], "msg": "Current password is incorrect", "type": "value_error"}],
        ) from None

    return TokenPairResponse(access_token=tokens.access_token, refresh_token=tokens.refresh_token)
