"""Product analytics (`app.analytics`): what may be sent, when it's off, and
that it can never break the action it describes."""

import uuid

import pytest

from app.analytics import (
    EVENT_PROPERTIES,
    DisabledAnalytics,
    Event,
    PostHogAnalytics,
    allowed_properties,
    before_send,
    build_analytics,
)
from app.config import Settings

_ANALYTICS_ID = uuid.UUID("6f1f6a52-6c43-4d3b-9b3b-0c3f1a7d8e90")


class FakePostHog:
    """Stands in for `posthog.Posthog`: records calls, or fails them."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.captured: list[tuple[str, str, dict]] = []
        self.flush_timeouts: list[float | None] = []

    def capture(self, event, *, distinct_id, properties):
        if self.fail:
            raise ConnectionError("posthog is unreachable")
        self.captured.append((event, distinct_id, properties))

    def flush(self, timeout_seconds=None):
        self.flush_timeouts.append(timeout_seconds)
        if self.fail:
            raise TimeoutError("flush timed out")


def _settings(**overrides) -> Settings:
    values = {
        "analytics_enabled": True,
        "posthog_project_api_key": "phc_test",
        "posthog_host": "https://eu.i.posthog.com",
    }
    values.update(overrides)
    return Settings(**values)


# --- What may be sent ---------------------------------------------------


def test_every_event_has_an_allowlist():
    assert set(EVENT_PROPERTIES) == set(Event)


def test_events_without_properties_send_none():
    for event in (Event.account_registered, Event.password_changed, Event.account_deleted):
        assert allowed_properties(event, {"email": "alice@example.com", "user_id": "x"}) == {}


def test_unlisted_properties_are_dropped():
    sent = allowed_properties(
        Event.item_saved,
        {
            "item_type": "file",
            "size_bytes": 1234,
            "filename": "secret-plans.pdf",
            "text": "my note",
            "url": "https://example.com/private",
            "query": "what I searched",
            "tag_name": "medical",
            "$ip": "203.0.113.7",
        },
    )

    assert sent == {"item_type": "file", "size_bytes": 1234}


@pytest.mark.parametrize(
    ("event", "properties"),
    [
        # Free text in place of a closed value.
        (Event.item_saved, {"item_type": "my holiday photos"}),
        (Event.language_changed, {"language": "Klingon"}),
        (Event.item_tags_changed, {"tag_visibility": "medical"}),
        # Counts must be non-negative integers, not text or flags.
        (Event.search_completed, {"result_count": "cats"}),
        (Event.search_completed, {"result_count": -1}),
        (Event.search_completed, {"result_count": True}),
    ],
)
def test_values_outside_their_allowed_set_are_dropped(event, properties):
    assert allowed_properties(event, properties) == {}


def test_before_send_keeps_only_allowed_and_library_properties():
    message = {
        "event": "item_favourite_changed",
        "distinct_id": str(_ANALYTICS_ID),
        "properties": {
            "action": "added",
            "item_type": "image",
            "$lib": "posthog-python",
            "$lib_version": "7.0.0",
            "$geoip_disable": True,
            # What the SDK adds about the server, or anything else.
            "$os": "Linux",
            "$python_version": "3.14.0",
            "$ip": "198.51.100.1",
            "email": "alice@example.com",
        },
    }

    sent = before_send(message)

    assert sent["properties"] == {
        "action": "added",
        "item_type": "image",
        "$lib": "posthog-python",
        "$lib_version": "7.0.0",
        "$geoip_disable": True,
    }


def test_before_send_drops_events_that_are_not_ours():
    assert before_send({"event": "$exception", "properties": {"$exception_message": "boom"}}) is None
    assert before_send({"event": "$pageview", "properties": {}}) is None


# --- When it's off ------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"analytics_enabled": False},
        {"posthog_project_api_key": ""},
        {"posthog_host": ""},
    ],
)
def test_disabled_unless_enabled_and_configured(overrides):
    assert isinstance(build_analytics(_settings(**overrides)), DisabledAnalytics)


def test_off_by_default():
    # What tests and unconfigured environments get.
    assert Settings().analytics_enabled is False
    assert isinstance(build_analytics(Settings()), DisabledAnalytics)


def test_enabled_and_configured_sends_to_posthog():
    analytics = build_analytics(_settings())

    assert isinstance(analytics, PostHogAnalytics)
    assert analytics.enabled


def test_disabled_analytics_does_nothing():
    analytics = DisabledAnalytics()

    analytics.capture(_ANALYTICS_ID, Event.item_saved, {"item_type": "text"})
    analytics.for_user(_ANALYTICS_ID).capture(Event.account_deleted)
    analytics.flush()


# --- Never in the way ---------------------------------------------------


def test_capture_sends_the_analytics_id_and_allowed_properties():
    client = FakePostHog()

    PostHogAnalytics(client).for_user(_ANALYTICS_ID).capture(
        Event.item_saved, {"item_type": "image", "size_bytes": 10, "filename": "x.png"}
    )

    assert client.captured == [("item_saved", str(_ANALYTICS_ID), {"item_type": "image", "size_bytes": 10})]


def test_a_failing_capture_never_raises():
    analytics = PostHogAnalytics(FakePostHog(fail=True))

    analytics.capture(_ANALYTICS_ID, Event.item_saved, {"item_type": "text"})


def test_flush_is_bounded_and_never_raises():
    client = FakePostHog(fail=True)

    PostHogAnalytics(client).flush()

    assert client.flush_timeouts == [Settings().analytics_flush_timeout_seconds]
    assert client.flush_timeouts[0] is not None


def test_the_real_client_is_bounded_and_flushes_within_its_timeout():
    """The SDK as configured: a bounded queue, no exception autocapture,
    no GeoIP, and a flush that returns within its timeout even when
    PostHog can't be reached."""
    import time

    settings = _settings(
        posthog_host="http://127.0.0.1:9",  # Nothing listens there.
        analytics_timeout_seconds=0.5,
        analytics_flush_timeout_seconds=0.5,
    )
    analytics = build_analytics(settings)
    assert isinstance(analytics, PostHogAnalytics)
    client = analytics._client
    assert client.disable_geoip is True
    assert client._analytics_lane.queue.maxsize == settings.analytics_max_queue_size

    analytics.capture(_ANALYTICS_ID, Event.search_completed, {"result_count": 3})
    started = time.monotonic()
    analytics.flush()

    # The flush's own budget plus one request's timeout, at most.
    assert time.monotonic() - started < 3
    client.shutdown()
