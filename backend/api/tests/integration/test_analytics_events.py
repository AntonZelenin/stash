"""Product analytics events (`app.analytics`): each action is captured
once, only after it's committed, for the user's analytics id, with only
allowed properties (`RecordingAnalytics` fails a test on anything else),
and analytics failing never fails the action."""

import pytest
from httpx import AsyncClient

from app.analytics import Event, PostHogAnalytics, get_analytics
from app.items.repos import ItemRepository, ItemSort
from app.main import app
from conftest import FakeEmbedder, RecordingAnalytics
from helpers import register_and_login, upload_file, upload_image

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def sqlite_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trigram, full-text and vector searches are Postgres only: on
    SQLite the user-text match returns every item of the user's, the others
    nothing (see `test_search.py` for the searches themselves)."""

    async def every_item(self, *, user_id, limit, filters, **kwargs):
        return await self.list_items(
            user_id=user_id, limit=limit, cursor_created_at=None, cursor_id=None, filters=filters, sort=ItemSort.newest
        )

    async def nothing(self, **kwargs):
        return []

    monkeypatch.setattr(ItemRepository, "search_by_user_text", every_item)
    monkeypatch.setattr(ItemRepository, "search_by_description", nothing)
    monkeypatch.setattr(ItemRepository, "search_by_chunks", nothing)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _analytics_id(client: AsyncClient, token: str) -> str:
    return (await client.get("/users/me", headers=_auth(token))).json()["analytics_id"]


async def _note(client: AsyncClient, token: str, text: str = "hello", **body) -> str:
    response = await client.post("/items/text", json={"text": text, **body}, headers=_auth(token))
    assert response.status_code == 202, response.text
    return response.json()["id"]


async def _tag_id(client: AsyncClient, token: str, item_id: str, name: str) -> str:
    response = await client.post(f"/items/{item_id}/tags", json={"name": name}, headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()["id"]


# --- Identity -------------------------------------------------------------


async def test_events_use_the_accounts_stable_analytics_id(client: AsyncClient, analytics: RecordingAnalytics):
    user_id, token = await register_and_login(client)
    analytics_id = await _analytics_id(client, token)
    # Signing in again (another device) gets the same id.
    again = (await client.post("/login", json={"email": "alice@example.com", "password": "correct-horse"})).json()
    assert await _analytics_id(client, again["access_token"]) == analytics_id

    await _note(client, token)

    assert analytics_id != user_id
    assert [(who, name) for who, name, _ in analytics.events] == [
        (analytics_id, "account_registered"),
        (analytics_id, "item_saved"),
    ]


async def test_each_account_has_its_own_analytics_id(client: AsyncClient, analytics: RecordingAnalytics):
    _, alice = await register_and_login(client, email="alice@example.com")
    _, bob = await register_and_login(client, email="bob@example.com")

    assert await _analytics_id(client, alice) != await _analytics_id(client, bob)


async def test_no_event_carries_the_email_or_account_id(client: AsyncClient, analytics: RecordingAnalytics):
    user_id, token = await register_and_login(client)
    await _note(client, token, "secret note text", tags=["medical"])
    await client.post("/search", json={"query": "secret query"}, headers=_auth(token))

    for who, _, properties in analytics.events:
        sent = f"{who} {properties}"
        for private in (user_id, "alice@example.com", "secret note text", "medical", "secret query"):
            assert private not in sent


# --- Registration and account ----------------------------------------------


async def test_failed_registration_sends_nothing(client: AsyncClient, analytics: RecordingAnalytics):
    await register_and_login(client)
    analytics.events.clear()

    taken = await client.post("/users", json={"email": "alice@example.com", "password": "another-pass"})

    assert taken.status_code == 409
    assert analytics.events == []


async def test_password_changed_only_when_it_was(client: AsyncClient, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)

    wrong = await client.post(
        "/users/me/password", json={"current_password": "nope-nope", "new_password": "new-password"}, headers=_auth(token)
    )
    assert wrong.status_code == 422
    assert analytics.named("password_changed") == []

    changed = await client.post(
        "/users/me/password",
        json={"current_password": "correct-horse", "new_password": "new-password"},
        headers=_auth(token),
    )
    assert changed.status_code == 200
    assert analytics.named("password_changed") == [{}]


async def test_account_deleted(client: AsyncClient, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)
    analytics_id = await _analytics_id(client, token)

    wrong = await client.post("/users/me/delete", json={"password": "wrong"}, headers=_auth(token))
    assert wrong.status_code == 422
    assert analytics.named("account_deleted") == []

    deleted = await client.post("/users/me/delete", json={"password": "correct-horse"}, headers=_auth(token))

    assert deleted.status_code == 204
    assert analytics.events[-1] == (analytics_id, "account_deleted", {})


async def test_language_is_kept_with_the_account(client: AsyncClient, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)

    for _ in range(2):
        response = await client.put("/users/me/language", json={"language": "uk"}, headers=_auth(token))
        assert response.status_code == 204
    invalid = await client.put("/users/me/language", json={"language": "xx"}, headers=_auth(token))

    assert invalid.status_code == 422
    assert (await client.get("/users/me", headers=_auth(token))).json()["language"] == "uk"
    # Choosing the same language again changes nothing.
    assert analytics.named("language_changed") == [{"language": "uk"}]


# --- Items -----------------------------------------------------------------


async def test_item_saved_for_notes_and_links(client: AsyncClient, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)

    await _note(client, token, "a note")
    await _note(client, token, "https://example.com/page")

    assert analytics.named("item_saved") == [{"item_type": "text"}, {"item_type": "link"}]


async def test_item_saved_for_uploads_once(client: AsyncClient, storage, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)

    image = await upload_image(client, storage, token, _PNG, filename="photo.png")
    await upload_file(client, storage, token, "notes.txt", b"plain text")
    # A retried finalize returns the same item: not saved again.
    replay = await client.post(f"/uploads/{image.json()['id']}/finalize", headers=_auth(token))

    assert replay.status_code == 202
    assert analytics.named("item_saved") == [
        {"item_type": "image", "size_bytes": len(_PNG)},
        {"item_type": "file", "size_bytes": len(b"plain text")},
    ]


async def test_rejected_upload_sends_nothing(client: AsyncClient, storage, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)

    response = await upload_image(client, storage, token, b"not an image at all", filename="x.png")

    assert response.status_code == 422
    assert analytics.named("item_saved") == []


async def test_tags_given_on_save_count_as_added_tags(client: AsyncClient, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)
    first = await _note(client, token, "first", tags=["work"])
    work = (await client.get("/tags", headers=_auth(token))).json()["tags"][0]["id"]
    await client.post("/tags/visibility", json={"tag_ids": [work], "hidden": True}, headers=_auth(token))
    analytics.events.clear()

    await _note(client, token, "second", tags=["work", "ideas"])

    assert first
    assert analytics.named("item_saved") == [{"item_type": "text"}]
    # One per tag, as if each had been added afterwards: the hidden one
    # counted as hidden.
    assert sorted(analytics.named("item_tags_changed"), key=lambda p: p["tag_visibility"]) == [
        {"action": "add", "tag_visibility": "hidden", "affected_item_count": 1},
        {"action": "add", "tag_visibility": "regular", "affected_item_count": 1},
    ]


async def test_description_edited_only_when_the_text_changes(client: AsyncClient, storage, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)
    note = await _note(client, token, "before")
    upload = await upload_file(client, storage, token, "report.txt", b"report")
    file_id = upload.json()["id"]

    async def edit(item_id: str, **body):
        response = await client.patch(f"/items/{item_id}", json=body, headers=_auth(token))
        assert response.status_code == 200, response.text

    await edit(note, text="after")
    await edit(note, text="after")  # Unchanged.
    await edit(file_id, filename="renamed.txt")  # Not the description.
    await edit(file_id, text="a caption")
    await edit(file_id, text="")  # Caption removed: still an edit.

    assert analytics.named("item_description_edited") == [
        {"item_type": "text"},
        {"item_type": "file"},
        {"item_type": "file"},
    ]


async def test_favourite_changes_are_counted_once(client: AsyncClient, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)
    item_id = await _note(client, token)

    for _ in range(2):
        await client.put(f"/items/{item_id}/favorite", headers=_auth(token))
    for _ in range(2):
        await client.delete(f"/items/{item_id}/favorite", headers=_auth(token))
    missing = await client.put("/items/00000000-0000-0000-0000-000000000000/favorite", headers=_auth(token))

    assert missing.status_code == 404
    assert analytics.named("item_favourite_changed") == [
        {"action": "added", "item_type": "text"},
        {"action": "removed", "item_type": "text"},
    ]


async def test_items_deleted_counts_by_type(client: AsyncClient, storage, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)
    notes = [await _note(client, token, f"note {i}") for i in range(2)]
    link = await _note(client, token, "https://example.com")
    image = (await upload_image(client, storage, token, _PNG, filename="p.png")).json()["id"]
    single = await _note(client, token, "single")

    bulk = await client.post(
        "/items/delete",
        json={"ids": [*notes, link, image, "00000000-0000-0000-0000-000000000000"]},
        headers=_auth(token),
    )
    await client.delete(f"/items/{single}", headers=_auth(token))
    # Already gone: nothing deleted, nothing counted.
    await client.delete(f"/items/{single}", headers=_auth(token))
    await client.post("/items/delete", json={"ids": notes}, headers=_auth(token))

    assert bulk.status_code == 204
    assert analytics.named("items_deleted") == [
        {"deleted_count": 4, "text_count": 2, "link_count": 1, "image_count": 1, "file_count": 0},
        {"deleted_count": 1, "text_count": 1, "link_count": 0, "image_count": 0, "file_count": 0},
    ]


# --- Tags --------------------------------------------------------------------


async def test_item_tags_changed_only_for_real_changes(client: AsyncClient, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)
    item_id = await _note(client, token)

    tag_id = await _tag_id(client, token, item_id, "work")
    await _tag_id(client, token, item_id, "Work")  # Already on it.
    await client.delete(f"/items/{item_id}/tags/{tag_id}", headers=_auth(token))
    await client.delete(f"/items/{item_id}/tags/{tag_id}", headers=_auth(token))  # Already off.

    assert analytics.named("item_tags_changed") == [
        {"action": "add", "tag_visibility": "regular", "affected_item_count": 1},
        {"action": "remove", "tag_visibility": "regular", "affected_item_count": 1},
    ]


async def test_removing_a_hidden_tag_counts_as_hidden(client: AsyncClient, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)
    item_id = await _note(client, token)
    tag_id = await _tag_id(client, token, item_id, "private")
    await client.post("/tags/visibility", json={"tag_ids": [tag_id], "hidden": True}, headers=_auth(token))
    analytics.events.clear()

    # Its last item: the tag goes with the link, after it was read.
    await client.delete(f"/items/{item_id}/tags/{tag_id}", headers=_auth(token))

    assert analytics.named("item_tags_changed") == [
        {"action": "remove", "tag_visibility": "hidden", "affected_item_count": 1}
    ]


async def test_hidden_tags_are_kept_with_the_account(client: AsyncClient, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)
    item_id = await _note(client, token)
    work = await _tag_id(client, token, item_id, "work")
    home = await _tag_id(client, token, item_id, "home")
    _, other_token = await register_and_login(client, email="bob@example.com")
    other_item = await _note(client, other_token)
    others = await _tag_id(client, other_token, other_item, "bobs")

    async def visibility(tag_ids, hidden, **extra):
        response = await client.post(
            "/tags/visibility", json={"tag_ids": tag_ids, "hidden": hidden, **extra}, headers=_auth(token)
        )
        assert response.status_code == 204, response.text

    await visibility([work, home, others], True)  # Bob's tag is ignored.
    await visibility([work], True)  # Already hidden.
    await visibility([home], False)

    hidden = (await client.get("/tags/hidden", headers=_auth(token))).json()["tags"]
    assert [tag["id"] for tag in hidden] == [work]
    listed = (await client.get("/tags", headers=_auth(token))).json()["tags"]
    assert {tag["name"]: tag["hidden"] for tag in listed} == {"home": False, "work": True}
    assert (await client.get("/tags/hidden", headers=_auth(other_token))).json()["tags"] == []
    assert analytics.named("tag_visibility_changed") == [
        {"visibility": "hidden", "affected_tag_count": 2},
        {"visibility": "visible", "affected_tag_count": 1},
    ]


async def test_hidden_tags_imported_from_a_device_are_not_counted(client: AsyncClient, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)
    item_id = await _note(client, token)
    tag_id = await _tag_id(client, token, item_id, "work")

    response = await client.post(
        "/tags/visibility",
        json={"tag_ids": [tag_id], "hidden": True, "imported_from_device": True},
        headers=_auth(token),
    )

    assert response.status_code == 204
    assert [tag["id"] for tag in (await client.get("/tags/hidden", headers=_auth(token))).json()["tags"]] == [tag_id]
    assert analytics.named("tag_visibility_changed") == []


# --- Search ------------------------------------------------------------------


async def test_search_completed_once_per_search(client: AsyncClient, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)
    await _note(client, token, "cats are great")

    await client.post("/search", json={"query": "cats"}, headers=_auth(token))
    # The client refreshing the same search (after an edit, a filter
    # change...): not another search.
    await client.post("/search", json={"query": "cats", "rerun": True, "favorite": True}, headers=_auth(token))
    # Listing isn't searching.
    await client.get("/items", headers=_auth(token))

    assert analytics.named("search_completed") == [{"result_count": 1}]


async def test_failed_search_is_not_counted(client: AsyncClient, embedder: FakeEmbedder, analytics: RecordingAnalytics):
    _, token = await register_and_login(client)
    embedder.fail = True

    response = await client.post("/search", json={"query": "cats"}, headers=_auth(token))

    assert response.status_code == 503
    assert analytics.named("search_completed") == []


# --- Failure isolation --------------------------------------------------------


class _BrokenPostHog:
    def capture(self, *args, **kwargs):
        raise ConnectionError("posthog is unreachable")

    def flush(self, timeout_seconds=None):
        raise TimeoutError()


async def test_actions_succeed_when_analytics_fails(client: AsyncClient, storage):
    app.dependency_overrides[get_analytics] = lambda: PostHogAnalytics(_BrokenPostHog())
    try:
        registered = await client.post("/users", json={"email": "carol@example.com", "password": "correct-horse"})
        assert registered.status_code == 201
        login = await client.post("/login", json={"email": "carol@example.com", "password": "correct-horse"})
        token = login.json()["access_token"]

        item_id = await _note(client, token, "still saved", tags=["kept"])
        assert (await client.put(f"/items/{item_id}/favorite", headers=_auth(token))).status_code == 204
        assert (await client.post("/search", json={"query": "saved"}, headers=_auth(token))).status_code == 200
        assert (await client.put("/users/me/language", json={"language": "uk"}, headers=_auth(token))).status_code == 204
        uploaded = await upload_image(client, storage, token, _PNG, filename="p.png")
        assert uploaded.status_code == 202
        assert (await client.delete(f"/items/{item_id}", headers=_auth(token))).status_code == 204
    finally:
        app.dependency_overrides.pop(get_analytics, None)

    listed = (await client.get("/items", headers=_auth(token))).json()["items"]
    assert [item["type"] for item in listed] == ["image"]


async def test_nothing_is_captured_when_analytics_is_off(client: AsyncClient):
    """Tests run with the default settings: analytics off."""
    assert get_analytics().enabled is False
    _, token = await register_and_login(client)
    await _note(client, token)


def test_every_event_is_exercised_here():
    """Keeps this file in step with `app.analytics.Event`."""
    import inspect
    import sys

    source = inspect.getsource(sys.modules[__name__])
    for event in Event:
        assert f'"{event.value}"' in source, event
