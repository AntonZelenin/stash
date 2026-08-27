from httpx import AsyncClient


async def _register(client: AsyncClient, email: str = "alice@example.com", password: str = "correct-horse"):
    return await client.post("/users", json={"email": email, "password": password})


async def _login(client: AsyncClient, email: str = "alice@example.com", password: str = "correct-horse"):
    return await client.post("/login", json={"email": email, "password": password})


async def test_register_creates_user(client: AsyncClient):
    response = await _register(client)

    assert response.status_code == 201
    assert "id" in response.json()


async def test_register_rejects_short_password(client: AsyncClient):
    response = await _register(client, password="short")

    assert response.status_code == 422


async def test_register_rejects_duplicate_email(client: AsyncClient):
    await _register(client)

    response = await _register(client)

    assert response.status_code == 409


async def test_login_returns_access_and_refresh_tokens(client: AsyncClient):
    await _register(client)

    response = await _login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["access_token"] != body["refresh_token"]


async def test_login_rejects_wrong_password(client: AsyncClient):
    await _register(client)

    response = await _login(client, password="wrong-password")

    assert response.status_code == 401


async def test_login_rejects_unknown_email(client: AsyncClient):
    response = await _login(client, email="nobody@example.com", password="whatever1")

    assert response.status_code == 401


async def test_protected_endpoint_rejects_missing_token(client: AsyncClient):
    response = await client.post("/search", json={"query": "foo"})

    assert response.status_code == 401


async def test_protected_endpoint_rejects_invalid_token(client: AsyncClient):
    response = await client.post(
        "/search", json={"query": "foo"}, headers={"Authorization": "Bearer not-a-real-token"}
    )

    assert response.status_code == 401


async def test_protected_endpoint_accepts_valid_token(client: AsyncClient):
    await _register(client)
    login_response = await _login(client)
    token = login_response.json()["access_token"]

    response = await client.post("/search", json={"query": "foo"}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


async def test_refresh_returns_new_token_pair(client: AsyncClient):
    await _register(client)
    login_response = await _login(client)
    refresh_token = login_response.json()["refresh_token"]

    response = await client.post("/refresh", json={"refresh_token": refresh_token})

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["refresh_token"] != refresh_token


async def test_refresh_new_access_token_is_usable(client: AsyncClient):
    await _register(client)
    login_response = await _login(client)
    refresh_token = login_response.json()["refresh_token"]

    refresh_response = await client.post("/refresh", json={"refresh_token": refresh_token})
    new_access_token = refresh_response.json()["access_token"]

    response = await client.post(
        "/search", json={"query": "foo"}, headers={"Authorization": f"Bearer {new_access_token}"}
    )

    assert response.status_code == 200


async def test_refresh_token_cannot_be_reused_after_rotation(client: AsyncClient):
    await _register(client)
    login_response = await _login(client)
    refresh_token = login_response.json()["refresh_token"]

    first = await client.post("/refresh", json={"refresh_token": refresh_token})
    assert first.status_code == 200

    second = await client.post("/refresh", json={"refresh_token": refresh_token})
    assert second.status_code == 401


async def test_refresh_rejects_unknown_token(client: AsyncClient):
    response = await client.post("/refresh", json={"refresh_token": "not-a-real-token"})

    assert response.status_code == 401
