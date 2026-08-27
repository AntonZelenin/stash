from httpx import AsyncClient


async def _register(client: AsyncClient, email: str = "alice@example.com", password: str = "correct-horse"):
    return await client.post("/users", json={"email": email, "password": password})


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


async def test_login_returns_access_token(client: AsyncClient):
    await _register(client)

    response = await client.post("/login", json={"email": "alice@example.com", "password": "correct-horse"})

    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_login_rejects_wrong_password(client: AsyncClient):
    await _register(client)

    response = await client.post("/login", json={"email": "alice@example.com", "password": "wrong-password"})

    assert response.status_code == 401


async def test_login_rejects_unknown_email(client: AsyncClient):
    response = await client.post("/login", json={"email": "nobody@example.com", "password": "whatever1"})

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
    login_response = await client.post("/login", json={"email": "alice@example.com", "password": "correct-horse"})
    token = login_response.json()["access_token"]

    response = await client.post("/search", json={"query": "foo"}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
