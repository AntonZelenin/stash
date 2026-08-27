from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.auth import LoginRequest, LoginResponse
from app.auth.security import create_access_token
from app.auth.services import AuthService, InvalidCredentialsError
from app.db import get_db_session

router = APIRouter(tags=["auth"])


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    response_model=LoginResponse,
    responses={
        401: {"description": "Invalid credentials"},
    },
)
async def login(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_db_session),
) -> LoginResponse:
    try:
        user = await AuthService(session).authenticate(payload.email, payload.password)
    except InvalidCredentialsError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials") from None

    return LoginResponse(access_token=create_access_token(user.id))
