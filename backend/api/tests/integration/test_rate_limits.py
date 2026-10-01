"""Rate limits and quotas as the endpoints enforce them. Each test turns on
only the limits it's about (`rate_limits`), with small windows, and moves
time with `clock`."""

import asyncio

import pytest
from httpx import AsyncClient, Response

from app.config import Settings
from app.items.repos import ItemRepository
from app.rate_limits.windows import parse_windows

from conftest import FakeClock, FakeObjectStorage
from helpers import register_and_login, start_upload, upload_file, upload_image

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_PDF_BYTES = b"%PDF-1.7\n" + b"x" * 32
_MP4_BYTES = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _assert_rate_limited(response: Response, *, retry_after: int | None = None) -> None:
    assert response.status_code == 429
    # One generic answer for every limit: nothing about which one, or how
    # much of it is left.
    assert response.json() == {"detail": "Too many requests. Please try again later."}
    assert int(response.headers["Retry-After"]) >= 1
    if retry_after is not None:
        assert int(response.headers["Retry-After"]) == retry_after


async def _login(client: AsyncClient, email: str = "alice@example.com", password: str = "correct-horse") -> Response:
    return await client.post("/login", json={"email": email, "password": password})


async def _register(client: AsyncClient, email: str, password: str = "correct-horse") -> Response:
    return await client.post("/users", json={"email": email, "password": password})


# ---- login ----


async def test_login_is_limited_per_ip_whatever_the_outcome(client: AsyncClient, rate_limits, clock: FakeClock):
    await _register(client, "alice@example.com")
    rate_limits(login_per_ip="3/1m")
    clock.advance(15)

    # Successful logins count too: logging in to one's own account mustn't
    # reset the protection.
    assert (await _login(client)).status_code == 200
    assert (await _login(client, password="wrong-password")).status_code == 401
    assert (await _login(client)).status_code == 200
    _assert_rate_limited(await _login(client), retry_after=45)


async def test_login_limit_resets_after_its_window(client: AsyncClient, rate_limits, clock: FakeClock):
    await _register(client, "alice@example.com")
    rate_limits(login_per_ip="2/1m")
    await _login(client)
    await _login(client)
    _assert_rate_limited(await _login(client))

    clock.advance(60)

    assert (await _login(client)).status_code == 200


async def test_login_limit_per_ip_is_per_ip(client: AsyncClient, client_from, rate_limits):
    await _register(client, "alice@example.com")
    rate_limits(login_per_ip="1/1m")
    await _login(client)
    _assert_rate_limited(await _login(client))

    assert (await _login(client_from("198.51.100.7"))).status_code == 200


async def test_forwarded_for_headers_dont_change_the_ip(client: AsyncClient, rate_limits):
    await _register(client, "alice@example.com")
    rate_limits(login_per_ip="1/1m")
    await _login(client)

    spoofed = await client.post(
        "/login",
        json={"email": "alice@example.com", "password": "correct-horse"},
        headers={"X-Forwarded-For": "198.51.100.99", "X-Real-IP": "198.51.100.98"},
    )

    _assert_rate_limited(spoofed)


async def test_failed_logins_block_the_account_from_that_ip_even_with_the_right_password(
    client: AsyncClient, rate_limits, clock: FakeClock
):
    await _register(client, "alice@example.com")
    rate_limits(login_failures_per_account_ip="3/5m")
    for _ in range(3):
        assert (await _login(client, password="wrong-password")).status_code == 401

    # A guess that happens to be right mustn't be told apart from a wrong one.
    _assert_rate_limited(await _login(client), retry_after=300)

    clock.advance(300)
    assert (await _login(client)).status_code == 200


async def test_failed_logins_for_one_account_dont_block_another(client: AsyncClient, rate_limits):
    await _register(client, "alice@example.com")
    await _register(client, "bob@example.com")
    rate_limits(login_failures_per_account_ip="2/5m")
    await _login(client, password="wrong-password")
    await _login(client, password="wrong-password")
    _assert_rate_limited(await _login(client))

    assert (await _login(client, email="bob@example.com")).status_code == 200


async def test_failed_logins_from_one_ip_dont_block_the_account_elsewhere(
    client: AsyncClient, client_from, rate_limits
):
    await _register(client, "alice@example.com")
    rate_limits(login_failures_per_account_ip="2/5m")
    await _login(client, password="wrong-password")
    await _login(client, password="wrong-password")
    _assert_rate_limited(await _login(client))

    assert (await _login(client_from("198.51.100.7"))).status_code == 200


async def test_failed_logins_are_limited_per_account_across_ips(client: AsyncClient, client_from, rate_limits):
    """Guessing spread over many IPs."""
    await _register(client, "alice@example.com")
    rate_limits(login_failures_per_account="3/1h")
    for index in range(3):
        response = await _login(client_from(f"198.51.100.{index}"), password="wrong-password")
        assert response.status_code == 401

    _assert_rate_limited(await _login(client_from("198.51.100.50")))
    # Case doesn't make it a different account to count.
    _assert_rate_limited(await _login(client_from("198.51.100.51"), email="ALICE@example.com"))


def _production_login_limits() -> dict[str, str]:
    settings = Settings()
    return {
        "login_per_ip": settings.login_limit_per_ip,
        "login_failures_per_account_ip": settings.login_failure_limit_per_account_ip,
        "login_failures_per_account": settings.login_failure_limit_per_account,
    }


def test_no_failed_login_limit_blocks_for_longer_than_an_hour():
    settings = Settings()
    per_account_ip = parse_windows(settings.login_failure_limit_per_account_ip)
    per_account = parse_windows(settings.login_failure_limit_per_account)

    assert max(window.seconds for window in per_account_ip + per_account) <= 3600
    # Stricter from one address than across all of them, window for window.
    assert per_account_ip[0].limit < per_account[0].limit
    assert per_account_ip[-1].limit < per_account[-1].limit


async def test_distributed_guessing_cant_lock_an_account_for_a_day(
    client: AsyncClient, client_from, rate_limits, clock: FakeClock
):
    """With the production limits: guesses from 200 addresses block the
    account's logins — the owner's too — but for at most an hour, and only
    while they keep coming."""
    await _register(client, "alice@example.com")
    rate_limits(**_production_login_limits())
    owner = client_from("198.51.100.1")

    guesses = [await _login(client_from(f"203.0.{n // 250}.{n % 250}"), password="guess") for n in range(200)]

    assert [response.status_code for response in guesses[:20]] == [401] * 20
    blocked = [response for response in guesses if response.status_code == 429]
    assert len(blocked) == 180
    assert max(int(response.headers["Retry-After"]) for response in blocked) <= 15 * 60
    _assert_rate_limited(await _login(owner))

    # The attacker keeps going every 15 minutes: the hourly window fills up.
    for _ in range(3):
        clock.advance(15 * 60)
        for n in range(20):
            await _login(client_from(f"192.0.2.{n}"), password="guess")
    blocked_for = int((await _login(owner)).headers["Retry-After"])
    assert blocked_for <= 3600

    clock.advance(blocked_for)
    assert (await _login(owner)).status_code == 200


async def test_one_address_guessing_blocks_only_itself(client: AsyncClient, client_from, rate_limits):
    await _register(client, "alice@example.com")
    rate_limits(**_production_login_limits())
    attacker = client_from("203.0.113.9")

    for _ in range(5):
        assert (await _login(attacker, password="guess")).status_code == 401
    _assert_rate_limited(await _login(attacker, password="guess"))

    assert (await _login(client_from("198.51.100.1"))).status_code == 200


async def test_successful_logins_dont_count_as_failures(client: AsyncClient, rate_limits):
    await _register(client, "alice@example.com")
    rate_limits(login_failures_per_account_ip="2/5m")

    for _ in range(5):
        assert (await _login(client)).status_code == 200
    assert (await _login(client, password="wrong-password")).status_code == 401
    assert (await _login(client)).status_code == 200


async def test_concurrent_guesses_cant_outrun_the_failure_limit(client: AsyncClient, rate_limits):
    """Failures are reserved before the password is checked, so a burst of
    parallel guesses gets no more checks than the limit."""
    await _register(client, "alice@example.com")
    rate_limits(login_failures_per_account_ip="3/5m")

    responses = await asyncio.gather(*(_login(client, password=f"guess-{index}") for index in range(12)))

    codes = sorted(response.status_code for response in responses)
    assert codes == [401] * 3 + [429] * 9


# ---- registration, refresh, password change ----


async def test_registration_is_limited_per_ip(client: AsyncClient, client_from, rate_limits):
    rate_limits(registration_per_ip="2/1h")
    assert (await _register(client, "a@example.com")).status_code == 201
    assert (await _register(client, "b@example.com")).status_code == 201
    _assert_rate_limited(await _register(client, "c@example.com"))

    assert (await _register(client_from("198.51.100.7"), "c@example.com")).status_code == 201


async def test_registration_is_limited_per_email(client: AsyncClient, client_from, rate_limits):
    """Probing whether an email is registered (409) counts too."""
    await _register(client, "alice@example.com")
    rate_limits(registration_per_email="2/1h")
    assert (await _register(client_from("198.51.100.1"), "alice@example.com")).status_code == 409
    assert (await _register(client_from("198.51.100.2"), "alice@example.com")).status_code == 409

    # Counted whatever the case it's written in.
    _assert_rate_limited(await _register(client_from("198.51.100.3"), "ALICE@example.com"))
    assert (await _register(client, "bob@example.com")).status_code == 201


async def test_token_refresh_is_limited_per_ip(client: AsyncClient, rate_limits):
    await _register(client, "alice@example.com")
    refresh_token = (await _login(client)).json()["refresh_token"]
    rate_limits(token_refresh_per_ip="2/5m")

    first = await client.post("/refresh", json={"refresh_token": refresh_token})
    assert first.status_code == 200
    assert (await client.post("/refresh", json={"refresh_token": "not-a-token"})).status_code == 401
    _assert_rate_limited(await client.post("/refresh", json={"refresh_token": first.json()["refresh_token"]}))


async def test_wrong_current_passwords_are_limited_per_user(client: AsyncClient, rate_limits):
    _, token = await register_and_login(client)
    _, other_token = await register_and_login(client, email="bob@example.com")
    rate_limits(password_change_failures_per_user="2/15m")

    async def change(token: str, current: str) -> Response:
        return await client.post(
            "/users/me/password",
            json={"current_password": current, "new_password": "new-password-1"},
            headers=_auth(token),
        )

    assert (await change(token, "wrong-1")).status_code == 422
    assert (await change(token, "wrong-2")).status_code == 422
    _assert_rate_limited(await change(token, "correct-horse"))
    # Another user isn't affected.
    assert (await change(other_token, "correct-horse")).status_code == 200


# ---- uploads ----


async def test_uploads_started_are_limited_per_user(client: AsyncClient, storage: FakeObjectStorage, rate_limits):
    _, alice = await register_and_login(client)
    _, bob = await register_and_login(client, email="bob@example.com")
    rate_limits(uploads_per_user="2/5m")
    for _ in range(2):
        response = await start_upload(client, alice, type="file", filename="a.bin", size_bytes=10)
        assert response.status_code == 201
    signed = len(storage.signed_uploads)

    _assert_rate_limited(await start_upload(client, alice, type="file", filename="a.bin", size_bytes=10))
    # Nothing was signed for the rejected one.
    assert len(storage.signed_uploads) == signed
    assert (await start_upload(client, bob, type="file", filename="a.bin", size_bytes=10)).status_code == 201


async def test_upload_bytes_quota(client: AsyncClient, storage: FakeObjectStorage, rate_limits, clock: FakeClock):
    _, token = await register_and_login(client)
    rate_limits(upload_bytes_per_user="100/1d")
    assert (await start_upload(client, token, type="file", filename="a.bin", size_bytes=60)).status_code == 201

    _assert_rate_limited(await start_upload(client, token, type="file", filename="b.bin", size_bytes=41))
    assert (await start_upload(client, token, type="file", filename="c.bin", size_bytes=40)).status_code == 201

    clock.advance(86_400)
    assert (await start_upload(client, token, type="file", filename="d.bin", size_bytes=100)).status_code == 201


async def test_ai_analysis_quota_counts_only_uploads_that_will_be_analyzed(
    client: AsyncClient, storage: FakeObjectStorage, rate_limits
):
    _, token = await register_and_login(client)
    rate_limits(ai_analyses_per_user="2/1d")
    assert (await upload_image(client, storage, token, _PNG_BYTES)).status_code == 202
    assert (await upload_file(client, storage, token, "doc.pdf", _PDF_BYTES)).status_code == 202

    _assert_rate_limited(await upload_file(client, storage, token, "more.pdf", _PDF_BYTES))
    _assert_rate_limited(await upload_image(client, storage, token, _PNG_BYTES))
    # Not analyzed, so not charged: still accepted.
    assert (await upload_file(client, storage, token, "archive.zip", b"PK\x03\x04" + b"x" * 20)).status_code == 202


async def test_a_video_costs_several_ai_analyses(client: AsyncClient, storage: FakeObjectStorage, rate_limits):
    """Its analysis sends many frames: `ai_analysis_video_cost` (5) of the
    quota, where an image or document costs 1."""
    assert Settings().ai_analysis_video_cost == 5
    _, token = await register_and_login(client)
    rate_limits(ai_analyses_per_user="6/1d")
    assert (await upload_file(client, storage, token, "clip.mp4", _MP4_BYTES)).status_code == 202
    assert (await upload_file(client, storage, token, "doc.pdf", _PDF_BYTES)).status_code == 202

    _assert_rate_limited(await upload_file(client, storage, token, "more.pdf", _PDF_BYTES))


async def test_a_video_that_does_not_fit_the_ai_analysis_quota_is_refused(
    client: AsyncClient, storage: FakeObjectStorage, rate_limits
):
    _, token = await register_and_login(client)
    rate_limits(ai_analyses_per_user="4/1d")

    _assert_rate_limited(await upload_file(client, storage, token, "clip.mp4", _MP4_BYTES))
    # Nothing was charged for it.
    for _ in range(4):
        assert (await upload_file(client, storage, token, "doc.pdf", _PDF_BYTES)).status_code == 202


async def test_invalid_uploads_charge_nothing(client: AsyncClient, rate_limits):
    _, token = await register_and_login(client)
    rate_limits(uploads_per_user="1/5m")
    for _ in range(3):
        response = await start_upload(client, token, type="image", size_bytes=10, content_type="image/svg+xml")
        assert response.status_code == 422

    assert (await start_upload(client, token, type="file", filename="a.bin", size_bytes=10)).status_code == 201


async def test_finalizing_again_costs_nothing(client: AsyncClient, storage: FakeObjectStorage, rate_limits):
    """Clients may retry a finalize whose response was lost."""
    _, token = await register_and_login(client)
    rate_limits(uploads_per_user="1/5m", ai_analyses_per_user="1/1d")
    started = await start_upload(client, token, type="image", size_bytes=len(_PNG_BYTES), content_type="image/png")
    storage.put(started.json()["upload"]["url"], started.json()["upload"]["headers"], _PNG_BYTES)

    for _ in range(3):
        response = await client.post(f"/uploads/{started.json()['upload_id']}/finalize", headers=_auth(token))
        assert response.status_code == 202


# ---- search, notes and edits ----


@pytest.fixture
def no_search_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Postgres-only search queries, stubbed for SQLite (see
    `test_search`): these tests are about whether search runs at all."""

    async def nothing(*_args, **_kwargs) -> list:
        return []

    for method in ("search_by_user_text", "search_by_description", "search_by_chunks"):
        monkeypatch.setattr(ItemRepository, method, nothing)


async def test_searches_are_limited_per_user(
    client: AsyncClient, rate_limits, clock: FakeClock, embedder, no_search_matches
):
    _, alice = await register_and_login(client)
    _, bob = await register_and_login(client, email="bob@example.com")
    rate_limits(searches_per_user="2/1m")
    for _ in range(2):
        assert (await client.post("/search", json={"query": "cats"}, headers=_auth(alice))).status_code == 200
    embedded = len(embedder.queries)

    _assert_rate_limited(await client.post("/search", json={"query": "cats"}, headers=_auth(alice)))
    # Rejected before anything was sent to OpenAI.
    assert len(embedder.queries) == embedded
    assert (await client.post("/search", json={"query": "cats"}, headers=_auth(bob))).status_code == 200

    clock.advance(60)
    assert (await client.post("/search", json={"query": "cats"}, headers=_auth(alice))).status_code == 200


async def test_notes_and_edits_are_limited_per_user(client: AsyncClient, rate_limits):
    _, token = await register_and_login(client)
    created = await client.post("/items/text", json={"text": "first"}, headers=_auth(token))
    rate_limits(item_writes_per_user="2/5m")

    assert (await client.post("/items/text", json={"text": "second"}, headers=_auth(token))).status_code == 202
    edited = await client.patch(f"/items/{created.json()['id']}", json={"text": "edited"}, headers=_auth(token))
    assert edited.status_code == 200

    _assert_rate_limited(await client.post("/items/text", json={"text": "third"}, headers=_auth(token)))
    _assert_rate_limited(
        await client.patch(f"/items/{created.json()['id']}", json={"text": "again"}, headers=_auth(token))
    )
    # Reading isn't limited.
    assert (await client.get("/items", headers=_auth(token))).status_code == 200


async def test_rate_limits_can_be_turned_off(client: AsyncClient):
    """The default for tests (`no_rate_limits`), and `RATE_LIMITS_ENABLED`."""
    await _register(client, "alice@example.com")
    for _ in range(30):
        assert (await _login(client, password="wrong-password")).status_code == 401
