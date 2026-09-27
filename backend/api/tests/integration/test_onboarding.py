"""The first-login welcome: `GET /users/me` says whether the user has
dismissed it (`onboarding_completed`) and which limits to show, and
`PUT /users/me/onboarding-completed` records the dismissal, for good."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.users.models import User
from helpers import register_and_login

_COMPLETE = "/users/me/onboarding-completed"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _me(client: AsyncClient, token: str) -> dict:
    response = await client.get("/users/me", headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()


async def _login(client: AsyncClient, email: str = "alice@example.com") -> str:
    response = await client.post("/login", json={"email": email, "password": "correct-horse"})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def test_a_new_user_has_not_completed_onboarding(client: AsyncClient):
    _, token = await register_and_login(client)

    assert (await _me(client, token))["onboarding_completed"] is False


async def test_onboarding_stays_pending_until_dismissed(client: AsyncClient):
    # Signing in again (another device, or after logging out) before
    # dismissing it still shows the welcome.
    await register_and_login(client)

    assert (await _me(client, await _login(client)))["onboarding_completed"] is False


async def test_dismissing_it_is_remembered_across_logins(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.put(_COMPLETE, headers=_auth(token))

    assert response.status_code == 204, response.text
    assert (await _me(client, token))["onboarding_completed"] is True
    # Stored with the account, not the session: a later login sees it too.
    assert (await _me(client, await _login(client)))["onboarding_completed"] is True


async def test_dismissing_it_again_is_harmless(client: AsyncClient, session: AsyncSession):
    _, token = await register_and_login(client)
    await client.put(_COMPLETE, headers=_auth(token))
    first = (await session.execute(select(User.onboarding_completed_at))).scalar_one()

    response = await client.put(_COMPLETE, headers=_auth(token))

    assert response.status_code == 204
    session.expire_all()
    assert (await session.execute(select(User.onboarding_completed_at))).scalar_one() == first
    assert (await _me(client, token))["onboarding_completed"] is True


async def test_it_is_per_user(client: AsyncClient):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")

    await client.put(_COMPLETE, headers=_auth(alice))

    assert (await _me(client, alice))["onboarding_completed"] is True
    assert (await _me(client, bob))["onboarding_completed"] is False


async def test_dismissing_requires_a_valid_token(client: AsyncClient):
    assert (await client.put(_COMPLETE)).status_code == 401
    assert (await client.put(_COMPLETE, headers=_auth("nope"))).status_code == 401


async def test_limits_are_the_ones_enforced(client: AsyncClient, rate_limits, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_file_upload_bytes", 7 * 1024 * 1024)
    monkeypatch.setattr(get_settings(), "max_image_upload_bytes", 3 * 1024 * 1024)
    _, token = await register_and_login(client)
    rate_limits(
        uploads_per_user="100/5m,1000/1d",
        upload_bytes_per_user=f"{5 * 1024**3}/1d",
        ai_analyses_per_user="300/1h,900/1d",
        # Not a user-facing quota: never reported.
        login_per_ip="20/1m,500/1d",
    )

    limits = (await _me(client, token))["limits"]

    assert limits == {
        "max_file_bytes": 7 * 1024 * 1024,
        "max_image_bytes": 3 * 1024 * 1024,
        "max_text_length": 100_000,
        "uploads_per_day": 1000,
        "upload_bytes_per_day": 5 * 1024**3,
        "ai_analyses_per_day": 900,
    }


async def test_quotas_without_a_daily_window_are_not_reported(client: AsyncClient, rate_limits):
    _, token = await register_and_login(client)
    rate_limits(uploads_per_user="100/5m", ai_analyses_per_user="")

    limits = (await _me(client, token))["limits"]

    assert limits["uploads_per_day"] is None
    assert limits["ai_analyses_per_day"] is None


async def test_quotas_are_not_reported_when_rate_limits_are_off(client: AsyncClient):
    # The `client` fixture turns rate limits off unless a test enables them.
    _, token = await register_and_login(client)

    limits = (await _me(client, token))["limits"]

    assert limits["uploads_per_day"] is None
    assert limits["upload_bytes_per_day"] is None
    assert limits["ai_analyses_per_day"] is None
    # Size limits always apply.
    assert limits["max_file_bytes"] == get_settings().max_file_upload_bytes
    assert limits["max_image_bytes"] == get_settings().max_image_upload_bytes
