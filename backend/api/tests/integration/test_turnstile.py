"""Cloudflare Turnstile on registration: every registration carries a token
that's verified server-side (see `conftest.FakeSiteverify`), and nothing
else about it is trusted."""

from httpx import AsyncClient
from sqlalchemy import select

from app.main import app
from app.turnstile import TurnstileVerifier, get_turnstile_verifier
from app.users.models import User

from conftest import FakeSiteverify
from helpers import register_and_login

_VERIFICATION_FAILED = [
    {"loc": ["body", "turnstile_token"], "msg": "Verification failed, try again", "type": "turnstile"}
]


async def _register(client: AsyncClient, token: str | None, email: str = "alice@example.com"):
    body = {"email": email, "password": "correct-horse"}
    if token is not None:
        body["turnstile_token"] = token
    return await client.post("/users", json=body)


async def _users(session) -> list[str]:
    return list((await session.execute(select(User.email))).scalars())


def _assert_verification_failed(response) -> None:
    # One generic answer, whatever was wrong with the token.
    assert response.status_code == 422
    assert response.json() == {"detail": _VERIFICATION_FAILED}


async def test_valid_token_registers(client: AsyncClient, turnstile: FakeSiteverify, session):
    response = await _register(client, turnstile.issue())

    assert response.status_code == 201
    assert await _users(session) == ["alice@example.com"]
    [sent] = turnstile.requests
    assert sent["secret"] == FakeSiteverify.SECRET
    assert sent["response"] == "token-0"
    assert sent["remoteip"] == "127.0.0.1"
    assert sent["idempotency_key"]


async def test_missing_token_is_refused_without_asking_cloudflare(
    client: AsyncClient, turnstile: FakeSiteverify, session
):
    _assert_verification_failed(await _register(client, None))
    _assert_verification_failed(await _register(client, ""))

    assert turnstile.requests == []
    assert await _users(session) == []


async def test_invalid_token_is_refused(client: AsyncClient, turnstile: FakeSiteverify, session):
    _assert_verification_failed(await _register(client, "forged-token"))

    assert await _users(session) == []


async def test_expired_token_is_refused(client: AsyncClient, turnstile: FakeSiteverify, session):
    token = turnstile.issue()
    turnstile.expire(token)

    _assert_verification_failed(await _register(client, token))
    assert await _users(session) == []


async def test_token_can_be_used_once(client: AsyncClient, turnstile: FakeSiteverify, session):
    token = turnstile.issue()

    assert (await _register(client, token)).status_code == 201
    _assert_verification_failed(await _register(client, token, email="bob@example.com"))
    assert await _users(session) == ["alice@example.com"]


async def test_token_solved_elsewhere_is_refused(client: AsyncClient, turnstile: FakeSiteverify):
    _assert_verification_failed(await _register(client, turnstile.issue(hostname="evil.example")))
    _assert_verification_failed(await _register(client, turnstile.issue(action="login")))


async def test_testing_key_answers_register(client: AsyncClient, turnstile: FakeSiteverify, session):
    # Cloudflare's test secret keys (the local stack's) echo no action and
    # hostname "example.com", whatever the widget was rendered with.
    turnstile.testing_key = True

    response = await _register(client, turnstile.issue())

    assert response.status_code == 201
    assert await _users(session) == ["alice@example.com"]


async def test_cloudflare_unreachable_fails_closed(client: AsyncClient, turnstile: FakeSiteverify, session):
    turnstile.down = True

    response = await _register(client, turnstile.issue())

    assert response.status_code == 503
    assert await _users(session) == []


async def test_taken_email_is_only_revealed_after_verification(client: AsyncClient, turnstile: FakeSiteverify):
    assert (await _register(client, turnstile.issue())).status_code == 201

    _assert_verification_failed(await _register(client, "forged-token"))
    assert (await _register(client, turnstile.issue())).status_code == 409


async def test_failed_challenges_count_against_registration_limits(
    client: AsyncClient, turnstile: FakeSiteverify, rate_limits
):
    rate_limits(registration_per_ip="2/1h")

    _assert_verification_failed(await _register(client, "forged-token"))
    _assert_verification_failed(await _register(client, "forged-token", email="bob@example.com"))
    response = await _register(client, turnstile.issue(), email="carol@example.com")

    assert response.status_code == 429
    # Rate limited before Cloudflare is asked.
    assert [sent["response"] for sent in turnstile.requests] == ["forged-token", "forged-token"]


async def test_only_registration_is_challenged(client: AsyncClient, turnstile: FakeSiteverify):
    assert (await _register(client, turnstile.issue())).status_code == 201

    login = await client.post("/login", json={"email": "alice@example.com", "password": "correct-horse"})

    assert login.status_code == 200
    assert len(turnstile.requests) == 1


async def test_disabled_turnstile_needs_no_token(client: AsyncClient):
    """Local development and tests (TURNSTILE_ENABLED=false)."""
    user_id, token = await register_and_login(client)

    assert user_id and token


async def test_unconfigured_secret_fails_closed(client: AsyncClient, session):
    """Turnstile on without a secret (a deployment missing its value):
    registration is refused, not let through unverified."""
    verifier = TurnstileVerifier(
        enabled=True, verify_url="https://challenges.example", timeout_seconds=1, allowed_hostnames=[], secret_key=lambda: ""
    )
    app.dependency_overrides[get_turnstile_verifier] = lambda: verifier

    response = await _register(client, "any-token")

    assert response.status_code == 503
    assert await _users(session) == []
