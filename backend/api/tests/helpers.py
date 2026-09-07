from httpx import AsyncClient


async def register_and_login(
    client: AsyncClient, email: str = "alice@example.com", password: str = "correct-horse"
) -> tuple[str, str]:
    """Registers and logs in a user, returning (user_id, access_token)."""
    register_response = await client.post("/users", json={"email": email, "password": password})
    login_response = await client.post("/login", json={"email": email, "password": password})
    return register_response.json()["id"], login_response.json()["access_token"]
