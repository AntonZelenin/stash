from uuid import uuid4

from fastapi import APIRouter, status

from app.schemas.users import UserCreateRequest, UserCreateResponse

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
async def create_user(payload: UserCreateRequest) -> UserCreateResponse:
    return UserCreateResponse(id=uuid4())
