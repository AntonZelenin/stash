"""Every request is logged once with its id, route, status and duration;
everything logged while handling it carries the same request id (and the
user id once authenticated), so a failure can be traced to its request."""

import logging

import pytest

from helpers import register_and_login


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.INFO)


def _records(caplog, message: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == message]


async def test_request_is_logged_with_route_status_and_user(client, caplog):
    user_id, token = await register_and_login(client)
    caplog.clear()

    response = await client.post("/items/text", json={"text": "hello"}, headers={"Authorization": f"Bearer {token}"})

    [completed] = _records(caplog, "Request completed")
    fields = completed.stash_fields
    assert (fields["method"], fields["route"], fields["status_code"]) == ("POST", "/items/text", 202)
    assert fields["duration_ms"] >= 0
    assert str(fields["user_id"]) == user_id
    assert str(fields["item_id"]) == response.json()["id"]

    # The service's own log shares the request id.
    [created] = _records(caplog, "Item created")
    assert created.stash_fields["request_id"] == fields["request_id"]
    assert created.stash_fields["item_type"].value == "text"


async def test_each_request_gets_its_own_id(client, caplog):
    await client.get("/items")
    await client.get("/items")

    first, second = _records(caplog, "Request completed")
    assert first.stash_fields["status_code"] == 401
    assert "user_id" not in first.stash_fields
    assert first.stash_fields["request_id"] != second.stash_fields["request_id"]


async def test_failed_login_is_logged_without_the_email(client, caplog):
    await client.post("/login", json={"email": "nobody@example.com", "password": "wrong-password"})

    [failed] = _records(caplog, "Login failed")
    assert failed.levelno == logging.WARNING
    assert failed.stash_fields["reason"] == "unknown_email"
    assert "nobody@example.com" not in caplog.text
