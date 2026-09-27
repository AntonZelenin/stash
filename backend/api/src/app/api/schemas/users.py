from uuid import UUID

from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, Field

from app.api.schemas.auth import MAX_PASSWORD_INPUT_LENGTH

# bcrypt only uses a password's first 72 bytes (and bcrypt 5 refuses longer
# ones outright), so a new password must fit in them: 72 ASCII characters,
# fewer in other scripts.
MAX_PASSWORD_BYTES = 72


def _fits_bcrypt(password: str) -> str:
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes (fewer characters in non-Latin scripts)")
    return password


NewPassword = Annotated[str, Field(min_length=8, max_length=MAX_PASSWORD_BYTES), AfterValidator(_fits_bcrypt)]


class UserCreateRequest(BaseModel):
    # EmailStr itself rejects addresses over 254 characters.
    email: EmailStr
    password: NewPassword
    # From the Turnstile widget on the sign-up form (see `app.turnstile`);
    # required unless Turnstile is disabled. Cloudflare's are at most 2,048
    # characters.
    turnstile_token: str | None = Field(default=None, max_length=2048)


class UserCreateResponse(BaseModel):
    id: UUID


class CurrentUserResponse(BaseModel):
    id: UUID
    email: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=MAX_PASSWORD_INPUT_LENGTH)
    new_password: NewPassword
