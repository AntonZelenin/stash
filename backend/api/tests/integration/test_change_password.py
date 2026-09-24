from httpx import AsyncClient

from helpers import register_and_login


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _change(client: AsyncClient, token: str, current: str = "correct-horse", new: str = "battery-staple"):
    return await client.post(
        "/users/me/password",
        json={"current_password": current, "new_password": new},
        headers=_auth(token),
    )


async def _login(client: AsyncClient, password: str):
    return await client.post("/login", json={"email": "alice@example.com", "password": password})


async def test_change_password(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await _change(client, token)

    assert response.status_code == 200, response.text
    assert (await _login(client, "correct-horse")).status_code == 401
    assert (await _login(client, "battery-staple")).status_code == 200


async def test_returned_tokens_keep_the_caller_signed_in(client: AsyncClient):
    _, token = await register_and_login(client)

    tokens = (await _change(client, token)).json()

    assert (await client.get("/items", headers=_auth(tokens["access_token"]))).status_code == 200
    refreshed = await client.post("/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200


async def test_other_sessions_are_signed_out(client: AsyncClient):
    _, token = await register_and_login(client)
    other = (await _login(client, "correct-horse")).json()

    assert (await _change(client, token)).status_code == 200

    assert (await client.get("/items", headers=_auth(token))).status_code == 401
    assert (await client.get("/items", headers=_auth(other["access_token"]))).status_code == 401
    refreshed = await client.post("/refresh", json={"refresh_token": other["refresh_token"]})
    assert refreshed.status_code == 401


async def test_wrong_current_password_is_rejected_on_that_field(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await _change(client, token, current="not-my-password")

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "current_password"]
    assert error["msg"] == "Current password is incorrect"
    # Nothing changed: the old password and the session still work.
    assert (await _login(client, "correct-horse")).status_code == 200
    assert (await client.get("/items", headers=_auth(token))).status_code == 200


async def test_new_password_must_be_valid(client: AsyncClient):
    _, token = await register_and_login(client)

    assert (await _change(client, token, new="short")).status_code == 422
    assert (await _change(client, token, new="x" * 73)).status_code == 422
    assert (await _login(client, "correct-horse")).status_code == 200


async def test_change_password_requires_auth(client: AsyncClient):
    response = await client.post(
        "/users/me/password", json={"current_password": "correct-horse", "new_password": "battery-staple"}
    )

    assert response.status_code == 401
