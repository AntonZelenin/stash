from fastapi import APIRouter, status

from app.schemas.auth import LoginRequest, LoginResponse

router = APIRouter(tags=["auth"])


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    response_model=LoginResponse,
    responses={
        401: {"description": "Invalid credentials"},
    },
)
async def login(payload: LoginRequest) -> LoginResponse:
    return LoginResponse(access_token="placeholder-token")
