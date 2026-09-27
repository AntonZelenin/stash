from pydantic import BaseModel, EmailStr, Field

# Longest password a login or password check accepts. Stored passwords are
# at most 72 bytes (see `users.MAX_PASSWORD_BYTES`), so anything much longer
# can't match; the cap only bounds the work done on a guess.
MAX_PASSWORD_INPUT_LENGTH = 256


class LoginRequest(BaseModel):
    # EmailStr itself rejects addresses over 254 characters.
    email: EmailStr
    password: str = Field(max_length=MAX_PASSWORD_INPUT_LENGTH)


class RefreshTokenRequest(BaseModel):
    # Issued tokens are 43 characters.
    refresh_token: str = Field(max_length=256)


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
