"""Maximum lengths of user-controlled text (422), and of request bodies
(413)."""

import json
import uuid

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient

from app.api.schemas.items import MAX_FILENAME_LENGTH, MAX_TEXT_LENGTH
from app.body_size import BodyLimit, BodyLimitedRoute, body_limit_of
from app.config import get_settings
from app.main import app
from app.tags.names import MAX_TAG_NAME_LENGTH, MAX_TAGS_PER_ITEM

from helpers import register_and_login, start_upload

# Outside the Basic Multilingual Plane: 4 bytes of UTF-8, 12 as the escaped
# surrogate pair ("\ud83d\ude00") clients that escape non-ASCII send.
EMOJI = "\U0001f600"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _long_email(length: int) -> str:
    # A valid shape (labels of at most 63 characters), just too long.
    domain = ".".join(["a" * 60] * ((length - 10) // 61 + 1))
    return ("user@" + domain)[: length - 4] + ".com"


# ---- accounts ----


async def test_rejects_overlong_email_on_registration_and_login(client: AsyncClient):
    email = _long_email(300)

    registered = await client.post("/users", json={"email": email, "password": "correct-horse"})
    logged_in = await client.post("/login", json={"email": email, "password": "correct-horse"})

    assert (registered.status_code, logged_in.status_code) == (422, 422)


async def test_new_password_must_fit_in_72_bytes(client: AsyncClient):
    # 40 characters, 80 bytes: bcrypt would refuse (or truncate) it.
    cyrillic = "пароль" * 6 + "пари"

    too_long = await client.post("/users", json={"email": "a@example.com", "password": cyrillic})
    fits = await client.post("/users", json={"email": "b@example.com", "password": "п" * 36})

    assert too_long.status_code == 422
    assert "72 bytes" in str(too_long.json()["detail"])
    assert fits.status_code == 201
    assert (await client.post("/login", json={"email": "b@example.com", "password": "п" * 36})).status_code == 200


async def test_rejects_overlong_new_password_on_change(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.post(
        "/users/me/password",
        json={"current_password": "correct-horse", "new_password": "x" * 73},
        headers=_auth(token),
    )

    assert response.status_code == 422


async def test_rejects_overlong_login_password_and_refresh_token(client: AsyncClient):
    login = await client.post("/login", json={"email": "a@example.com", "password": "x" * 257})
    refresh = await client.post("/refresh", json={"refresh_token": "x" * 257})

    assert (login.status_code, refresh.status_code) == (422, 422)


# ---- items ----


async def test_note_text_limit(client: AsyncClient):
    _, token = await register_and_login(client)

    longest = await client.post("/items/text", json={"text": "x" * 100_000}, headers=_auth(token))
    too_long = await client.post("/items/text", json={"text": "x" * 100_001}, headers=_auth(token))

    assert (longest.status_code, too_long.status_code) == (202, 422)


async def test_edit_text_limit(client: AsyncClient):
    _, token = await register_and_login(client)
    created = await client.post("/items/text", json={"text": "note"}, headers=_auth(token))

    response = await client.patch(
        f"/items/{created.json()['id']}", json={"text": "x" * 100_001}, headers=_auth(token)
    )

    assert response.status_code == 422


async def test_upload_caption_filename_and_tag_limits(client: AsyncClient):
    _, token = await register_and_login(client)

    caption = await start_upload(client, token, type="file", filename="a.txt", size_bytes=1, text="x" * 100_001)
    filename = await start_upload(client, token, type="file", filename="x" * 1_001, size_bytes=1)
    tag = await start_upload(client, token, type="file", filename="a.txt", size_bytes=1, tags=["x" * 201])

    assert (caption.status_code, filename.status_code, tag.status_code) == (422, 422, 422)


async def test_tag_name_limits(client: AsyncClient):
    _, token = await register_and_login(client)

    on_create = await client.post("/items/text", json={"text": "note", "tags": ["x" * 201]}, headers=_auth(token))
    listing = await client.get("/tags", params={"query": "x" * 201}, headers=_auth(token))

    assert (on_create.status_code, listing.status_code) == (422, 422)


async def test_search_query_limit(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.post("/search", json={"query": "x" * 1_001}, headers=_auth(token))

    assert response.status_code == 422


async def test_listing_cursor_limit(client: AsyncClient):
    _, token = await register_and_login(client)

    response = await client.get("/items", params={"cursor": "x" * 513}, headers=_auth(token))

    assert response.status_code == 422


# ---- request bodies ----

_JSON = {"Content-Type": "application/json"}


def _escaped(value: str) -> str:
    """`value` as a JSON string with every character escaped as `\\u00XX`
    (6 bytes each: the longest a BMP character gets)."""
    return '"' + "".join(f"\\u{ord(char):04x}" for char in value) + '"'


async def test_longest_note_in_the_widest_encoding_fits(client: AsyncClient):
    """A 100,000-character note of emoji, each escaped as a surrogate pair
    (12 bytes, the most a character takes in JSON), with 20 tags."""
    _, token = await register_and_login(client)
    tags = [f"{EMOJI * (MAX_TAG_NAME_LENGTH - 3)} {n}" for n in range(MAX_TAGS_PER_ITEM)]
    body = json.dumps({"text": EMOJI * MAX_TEXT_LENGTH, "tags": tags})
    assert len(body) > 12 * MAX_TEXT_LENGTH

    response = await client.post("/items/text", content=body, headers={**_auth(token), **_JSON})

    assert response.status_code == 202


async def test_longest_note_as_raw_utf8_fits(client: AsyncClient):
    _, token = await register_and_login(client)
    body = json.dumps({"text": EMOJI * MAX_TEXT_LENGTH}, ensure_ascii=False).encode()
    assert len(body) > 4 * MAX_TEXT_LENGTH

    response = await client.post("/items/text", content=body, headers={**_auth(token), **_JSON})

    assert response.status_code == 202


async def test_longest_caption_filename_and_tags_fit_on_upload_and_edit(client: AsyncClient):
    _, token = await register_and_login(client)
    upload = json.dumps(
        {
            "type": "file",
            "size_bytes": 10,
            "filename": EMOJI * (MAX_FILENAME_LENGTH - 4) + ".txt",
            "text": EMOJI * MAX_TEXT_LENGTH,
            "tags": [f"{EMOJI * (MAX_TAG_NAME_LENGTH - 3)} {n}" for n in range(MAX_TAGS_PER_ITEM)],
        }
    )

    started = await client.post("/uploads", content=upload, headers={**_auth(token), **_JSON})
    created = await client.post("/items/text", json={"text": "note"}, headers=_auth(token))
    edited = await client.patch(
        f"/items/{created.json()['id']}",
        content=json.dumps({"text": EMOJI * MAX_TEXT_LENGTH}),
        headers={**_auth(token), **_JSON},
    )

    assert (started.status_code, edited.status_code) == (201, 200)


async def test_the_largest_possible_content_body_is_under_the_limit(client: AsyncClient):
    """Every field of `POST /uploads` (the largest content request) at its
    maximum length, every character 12 bytes: rejected for what's in it,
    never for its size, with room to spare."""
    _, token = await register_and_login(client)
    body = json.dumps(
        {
            "type": "file",
            "size_bytes": 10,
            "filename": EMOJI * MAX_FILENAME_LENGTH,
            "content_type": EMOJI * 255,
            "text": EMOJI * MAX_TEXT_LENGTH,
            "tags": [EMOJI * (4 * MAX_TAG_NAME_LENGTH)] * MAX_TAGS_PER_ITEM,
        }
    )
    assert 1_260_000 < len(body) <= 0.65 * get_settings().max_content_request_body_bytes

    response = await client.post("/uploads", content=body, headers={**_auth(token), **_JSON})

    assert response.status_code == 422


async def test_the_largest_possible_small_bodies_are_under_the_default_limit(client: AsyncClient):
    _, token = await register_and_login(client)
    ids = ",".join(_escaped(str(uuid.uuid4())) for _ in range(100))
    delete = f'{{"ids": [{ids}]}}'
    registration = json.dumps(
        {"email": "a@" + "b" * 60 + ".com", "password": EMOJI * 72, "turnstile_token": EMOJI * 2048}
    )
    # `limit` 0 is invalid: rejected once parsed, before any search runs.
    search = json.dumps({"query": EMOJI * 1_000, "tag_ids": [str(uuid.uuid4())] * 20, "limit": 0})
    assert max(len(delete), len(registration), len(search)) <= get_settings().max_request_body_bytes / 2

    deleted = await client.post("/items/delete", content=delete, headers={**_auth(token), **_JSON})
    registered = await client.post("/users", content=registration, headers=_JSON)
    searched = await client.post("/search", content=search, headers={**_auth(token), **_JSON})

    assert 413 not in (deleted.status_code, registered.status_code, searched.status_code)


async def test_rejects_small_body_over_its_limit_by_declared_length(client: AsyncClient):
    """Sign-in takes a few hundred bytes: far less than a note may."""
    body = b'{"email": "a@example.com", "password": "' + b"x" * get_settings().max_request_body_bytes + b'"}'

    response = await client.post("/login", content=body, headers=_JSON)

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is too large"}


async def test_rejects_content_body_over_its_limit_by_declared_length(client: AsyncClient):
    _, token = await register_and_login(client)
    body = b'{"text": "' + b"x" * get_settings().max_content_request_body_bytes + b'"}'

    response = await client.post("/items/text", content=body, headers={**_auth(token), **_JSON})

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is too large"}


@pytest.mark.parametrize(
    ("path", "limit"), [("/search", "max_request_body_bytes"), ("/items/text", "max_content_request_body_bytes")]
)
async def test_rejects_streamed_body_over_its_limit(client: AsyncClient, path: str, limit: str):
    """Without a Content-Length (chunked), the body is counted as it's read."""
    _, token = await register_and_login(client)
    chunk = b"x" * (getattr(get_settings(), limit) // 2)

    async def chunks():
        for _ in range(3):
            yield chunk

    response = await client.post(path, content=chunks(), headers={**_auth(token), **_JSON})

    assert response.status_code == 413
    assert "content-length" not in response.request.headers


def _api_routes(routes) -> list[APIRoute]:
    """The app's API routes, including those of included routers (which
    FastAPI keeps in their router)."""
    found = []
    for route in routes:
        if (included := getattr(route, "original_router", None)) is not None:
            found.extend(_api_routes(included.routes))
        elif isinstance(route, APIRoute):
            found.append(route)
    return found


def test_every_route_limits_its_body_and_only_content_routes_take_more():
    routes = [route for route in _api_routes(app.routes) if route.path != "/health"]
    content = {
        (method, route.path)
        for route in routes
        if body_limit_of(route.endpoint) is BodyLimit.content
        for method in route.methods
    }

    assert len(routes) > 20
    assert all(isinstance(route, BodyLimitedRoute) for route in routes)
    assert content == {("POST", "/items/text"), ("POST", "/uploads"), ("PATCH", "/items/{item_id}")}


async def test_413_answers_carry_cors_headers(client: AsyncClient):
    """So the browser app can read them, not see a network error."""
    origin = get_settings().cors_allowed_origins[0]

    response = await client.post(
        "/login",
        content=b"x" * (get_settings().max_request_body_bytes + 1),
        headers={"Origin": origin, "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == origin
