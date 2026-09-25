from httpx import AsyncClient, Response


async def register_and_login(
    client: AsyncClient, email: str = "alice@example.com", password: str = "correct-horse"
) -> tuple[str, str]:
    """Registers and logs in a user, returning (user_id, access_token)."""
    register_response = await client.post("/users", json={"email": email, "password": password})
    login_response = await client.post("/login", json={"email": email, "password": password})
    return register_response.json()["id"], login_response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def start_upload(client: AsyncClient, token: str, **body) -> Response:
    return await client.post("/uploads", json=body, headers=_auth(token))


async def finalize_upload(client: AsyncClient, token: str, upload_id: str) -> Response:
    return await client.post(f"/uploads/{upload_id}/finalize", headers=_auth(token))


async def upload(client: AsyncClient, storage, token: str, *, data: bytes, **body) -> Response:
    """The whole client-side flow: start the upload, send `data` straight
    to (fake) storage with the URL it returned, finalize. Returns the
    finalize response, or the start response if starting failed."""
    body.setdefault("size_bytes", len(data))
    started = await start_upload(client, token, **body)
    if started.status_code != 201:
        return started
    presigned = started.json()["upload"]
    storage.put(presigned["url"], presigned["headers"], data)
    return await finalize_upload(client, token, started.json()["upload_id"])


async def upload_image(
    client: AsyncClient, storage, token: str, data: bytes, *, content_type: str = "image/png", **fields
) -> Response:
    return await upload(client, storage, token, type="image", data=data, content_type=content_type, **fields)


async def upload_file(client: AsyncClient, storage, token: str, filename: str, data: bytes, **fields) -> Response:
    return await upload(client, storage, token, type="file", data=data, filename=filename, **fields)
