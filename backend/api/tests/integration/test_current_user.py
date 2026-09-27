from httpx import AsyncClient

from helpers import register_and_login


async def test_returns_the_signed_in_users_account(client: AsyncClient):
    user_id, token = await register_and_login(client, email="alice@example.com")

    response = await client.get("/users/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    assert response.json() == {"id": user_id, "email": "alice@example.com"}


async def test_requires_a_valid_token(client: AsyncClient):
    assert (await client.get("/users/me")).status_code == 401
    assert (await client.get("/users/me", headers={"Authorization": "Bearer nope"})).status_code == 401
