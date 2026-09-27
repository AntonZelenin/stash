from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.auth import LoginRequest, RefreshTokenRequest, TokenPairResponse
from app.auth.services import AuthService, InvalidCredentialsError, InvalidRefreshTokenError
from app.body_size import BodyLimitedRoute
from app.db import DbSession
from app.rate_limits.limiter import Charge, RateLimiter, client_ip, get_rate_limiter

router = APIRouter(tags=["auth"], route_class=BodyLimitedRoute)

_RATE_LIMITED = {429: {"description": "Too many requests; retry after `Retry-After` seconds"}}


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    response_model=TokenPairResponse,
    responses={
        401: {"description": "Invalid credentials"},
        **_RATE_LIMITED,
    },
)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = DbSession,
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> TokenPairResponse:
    ip = client_ip(request)
    account = payload.email.strip().lower()
    limits = limiter.limits
    # A failed attempt is reserved before the password is checked, and
    # refunded if it was right: concurrent guesses can't all be checked
    # before any of them counts. Every attempt counts for the IP, successful
    # ones too, so logging in to an account of one's own resets nothing.
    failures = (
        Charge(limits.login_failures_per_account_ip, f"{account}|{ip}"),
        Charge(limits.login_failures_per_account, account),
    )
    await limiter.consume(Charge(limits.login_per_ip, ip), *failures)

    service = AuthService(session)
    try:
        user = await service.authenticate(payload.email, payload.password)
    except InvalidCredentialsError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials") from None

    await limiter.refund(*failures)
    tokens = await service.issue_tokens(user)
    return TokenPairResponse(access_token=tokens.access_token, refresh_token=tokens.refresh_token)


@router.post(
    "/refresh",
    status_code=status.HTTP_200_OK,
    response_model=TokenPairResponse,
    responses={
        401: {"description": "Invalid refresh token"},
        **_RATE_LIMITED,
    },
)
async def refresh(
    payload: RefreshTokenRequest,
    request: Request,
    session: AsyncSession = DbSession,
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> TokenPairResponse:
    await limiter.consume(Charge(limiter.limits.token_refresh_per_ip, client_ip(request)))
    try:
        tokens = await AuthService(session).refresh(payload.refresh_token)
    except InvalidRefreshTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from None

    return TokenPairResponse(access_token=tokens.access_token, refresh_token=tokens.refresh_token)
