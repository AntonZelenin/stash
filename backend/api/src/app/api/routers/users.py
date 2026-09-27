from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.auth import TokenPairResponse
from app.api.schemas.items import MAX_TEXT_LENGTH
from app.api.schemas.users import (
    ChangePasswordRequest,
    CurrentUserResponse,
    UserCreateRequest,
    UserCreateResponse,
    UserLimits,
)
from app.auth.services import AuthService, IncorrectPasswordError
from app.body_size import BodyLimitedRoute
from app.config import get_settings
from app.db import DbSession
from app.dependencies import get_current_user
from app.rate_limits.limiter import Charge, Limit, RateLimiter, client_ip, get_rate_limiter
from app.turnstile import TurnstileUnavailableError, TurnstileVerifier, get_turnstile_verifier
from app.users.models import User
from app.users.services import EmailAlreadyRegisteredError, UserService

router = APIRouter(tags=["users"], route_class=BodyLimitedRoute)


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    response_model=UserCreateResponse,
    responses={
        409: {"description": "User already exists"},
        422: {"description": "Invalid request, or the Turnstile token isn't valid"},
        429: {"description": "Too many requests; retry after `Retry-After` seconds"},
        503: {"description": "Registration can't be verified right now"},
    },
)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    session: AsyncSession = DbSession,
    limiter: RateLimiter = Depends(get_rate_limiter),
    turnstile: TurnstileVerifier = Depends(get_turnstile_verifier),
) -> UserCreateResponse:
    # Every attempt counts, taken or not: a 409 says the email is
    # registered, so this also slows down probing for accounts. Counted
    # before Turnstile, so failed challenges count too (and Cloudflare
    # isn't asked more often than this allows).
    await limiter.consume(
        Charge(limiter.limits.registration_per_ip, client_ip(request)),
        Charge(limiter.limits.registration_per_email, payload.email.strip().lower()),
    )
    # Before anything about the email is looked at, so only a verified
    # registration learns whether it's taken.
    try:
        verified = await turnstile.verify(
            payload.turnstile_token, remote_ip=request.client.host if request.client else None
        )
    except TurnstileUnavailableError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Registration is unavailable, try again later") from None
    if not verified:
        # One answer for a missing, invalid, expired or reused token.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"loc": ["body", "turnstile_token"], "msg": "Verification failed, try again", "type": "turnstile"}],
        )
    try:
        user = await UserService(session).register(payload.email, payload.password)
    except EmailAlreadyRegisteredError:
        raise HTTPException(status.HTTP_409_CONFLICT, "User already exists") from None

    return UserCreateResponse(id=user.id)


@router.get(
    "/users/me",
    response_model=CurrentUserResponse,
    responses={401: {"description": "Unauthorized"}},
)
async def get_me(
    current_user: User = Depends(get_current_user),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> CurrentUserResponse:
    return CurrentUserResponse(
        id=current_user.id,
        email=current_user.email,
        onboarding_completed=current_user.onboarding_completed_at is not None,
        limits=_user_limits(limiter),
    )


def _user_limits(limiter: RateLimiter) -> UserLimits:
    """What `limiter` and the settings actually enforce, so clients never
    show numbers of their own. Only what users run into saving things:
    nothing about logins, registration or the infrastructure."""
    settings = get_settings()

    def per_day(limit: Limit) -> int | None:
        return limit.per_day if limiter.enabled else None

    return UserLimits(
        max_file_bytes=settings.max_file_upload_bytes,
        max_image_bytes=settings.max_image_upload_bytes,
        max_text_length=MAX_TEXT_LENGTH,
        uploads_per_day=per_day(limiter.limits.uploads_per_user),
        upload_bytes_per_day=per_day(limiter.limits.upload_bytes_per_user),
        ai_analyses_per_day=per_day(limiter.limits.ai_analyses_per_user),
    )


@router.put(
    "/users/me/onboarding-completed",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"description": "Unauthorized"}},
)
async def complete_onboarding(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
) -> None:
    await UserService(session).complete_onboarding(current_user)


@router.post(
    "/users/me/password",
    status_code=status.HTTP_200_OK,
    response_model=TokenPairResponse,
    responses={
        401: {"description": "Unauthorized"},
        422: {"description": "Current password is incorrect, or the new one is invalid"},
        429: {"description": "Too many requests; retry after `Retry-After` seconds"},
    },
)
async def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> TokenPairResponse:
    # Reserved, then refunded if the current password was right, like a
    # login: a stolen access token mustn't allow guessing the password.
    failure = Charge(limiter.limits.password_change_failures_per_user, str(current_user.id))
    await limiter.consume(failure)
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

    await limiter.refund(failure)
    return TokenPairResponse(access_token=tokens.access_token, refresh_token=tokens.refresh_token)
