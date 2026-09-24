from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.users import UserCreateRequest, UserCreateResponse
from app.db import DbSession
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
