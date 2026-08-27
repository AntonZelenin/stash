from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.auth import LoginRequest, RefreshTokenRequest, TokenPairResponse
from app.auth.services import AuthService, InvalidCredentialsError, InvalidRefreshTokenError
from app.db import get_db_session

router = APIRouter(tags=["auth"])


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    response_model=TokenPairResponse,
    responses={
        401: {"description": "Invalid credentials"},
    },
)
async def login(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_db_session),
) -> TokenPairResponse:
    service = AuthService(session)
    try:
        user = await service.authenticate(payload.email, payload.password)
    except InvalidCredentialsError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials") from None

    tokens = await service.issue_tokens(user)
    return TokenPairResponse(access_token=tokens.access_token, refresh_token=tokens.refresh_token)


@router.post(
    "/refresh",
    status_code=status.HTTP_200_OK,
    response_model=TokenPairResponse,
    responses={
        401: {"description": "Invalid refresh token"},
    },
)
async def refresh(
    payload: RefreshTokenRequest,
    session: AsyncSession = Depends(get_db_session),
) -> TokenPairResponse:
    try:
        tokens = await AuthService(session).refresh(payload.refresh_token)
    except InvalidRefreshTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from None

    return TokenPairResponse(access_token=tokens.access_token, refresh_token=tokens.refresh_token)
