"""Product analytics: what users do, sent to PostHog (`posthog` SDK).

Not operational telemetry (that's `stash_shared.metrics`, logs, traces):
these events answer product questions (activation, retention, which
features are used). See "Product analytics" in docs/architecture.md.

Rules every event follows:

- One place per action. An event is captured by the service that commits
  the action, right after its commit, so it only ever describes something
  that happened. Failed, rolled-back and no-op requests send nothing.
- Who: the user's `analytics_id`, a random id kept only for this (never
  the account id, never an email). The clients identify with the same id
  (`GET /users/me`), so frontend and backend events of an account are one
  person in PostHog, on every device.
- What: only the properties `EVENT_PROPERTIES` allows for the event, and
  for string properties only the values `ALLOWED_VALUES` lists, so no free
  text (content, filenames, queries, tag names, URLs...) can get in. The
  check runs on capture and again in the SDK's `before_send` hook, which
  also strips whatever the SDK itself adds beyond `SDK_PROPERTIES` (OS,
  runtime version...). GeoIP enrichment is disabled; the request IP PostHog
  sees is the API's own, never the user's.
- Never in the way: capturing only puts the event on the SDK's bounded
  in-memory queue (`analytics_max_queue_size`; when it's full, events are
  dropped) and never raises; sending happens on the SDK's consumer thread,
  each request bounded by `analytics_timeout_seconds`. The Lambda handler
  flushes at the end of every invocation (`flush`, bounded by
  `analytics_flush_timeout_seconds`), since the environment is frozen once
  it returns and the consumer thread can't be relied on to run after that.

Off (`DisabledAnalytics`, nothing sent or queued) unless
`ANALYTICS_ENABLED=true` and both `POSTHOG_PROJECT_API_KEY` and
`POSTHOG_HOST` are set.
"""

import uuid
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from typing import Any

from stash_shared.log import get_logger

from app.config import Settings, get_settings

logger = get_logger(__name__)


class Event(StrEnum):
    account_registered = "account_registered"
    item_saved = "item_saved"
    search_completed = "search_completed"
    item_description_edited = "item_description_edited"
    item_tags_changed = "item_tags_changed"
    tag_visibility_changed = "tag_visibility_changed"
    item_favourite_changed = "item_favourite_changed"
    items_deleted = "items_deleted"
    password_changed = "password_changed"
    account_deleted = "account_deleted"
    language_changed = "language_changed"


# The only properties each event may carry.
EVENT_PROPERTIES: dict[Event, frozenset[str]] = {
    Event.account_registered: frozenset(),
    # `size_bytes`: images and files only.
    Event.item_saved: frozenset({"item_type", "size_bytes"}),
    Event.search_completed: frozenset({"result_count"}),
    Event.item_description_edited: frozenset({"item_type"}),
    Event.item_tags_changed: frozenset({"action", "tag_visibility", "affected_item_count"}),
    Event.tag_visibility_changed: frozenset({"visibility", "affected_tag_count"}),
    Event.item_favourite_changed: frozenset({"action", "item_type"}),
    Event.items_deleted: frozenset(
        {"deleted_count", "text_count", "link_count", "image_count", "file_count"}
    ),
    Event.password_changed: frozenset(),
    Event.account_deleted: frozenset(),
    Event.language_changed: frozenset({"language"}),
}

# Every string property's possible values (closed sets). A property not
# listed here must be a non-negative integer.
ALLOWED_VALUES: dict[str, frozenset[str]] = {
    "item_type": frozenset({"text", "link", "image", "file"}),
    # item_tags_changed: "add"/"remove"; item_favourite_changed: "added"/"removed".
    "action": frozenset({"add", "remove", "added", "removed"}),
    "tag_visibility": frozenset({"regular", "hidden"}),
    "visibility": frozenset({"hidden", "visible"}),
    "language": frozenset({"en", "uk"}),
}

# What the SDK adds itself that may stay: library name/version and the
# GeoIP opt-out.
SDK_PROPERTIES = frozenset({"$lib", "$lib_version", "$geoip_disable"})


def allowed_properties(event: str, properties: Mapping[str, Any] | None) -> dict[str, Any]:
    """`properties` reduced to what `event` may carry: unknown properties,
    and values outside their allowed set or type, are dropped (and logged,
    by name only). Raises `ValueError` for an unknown event."""
    allowed = EVENT_PROPERTIES[Event(event)]
    kept: dict[str, Any] = {}
    for name, value in (properties or {}).items():
        if name in allowed and _valid_value(name, value):
            kept[name] = value
        else:
            logger.warning("Analytics property dropped", analytics_event=event, property=name)
    return kept


def _valid_value(name: str, value: Any) -> bool:
    if name in ALLOWED_VALUES:
        return isinstance(value, str) and value in ALLOWED_VALUES[name]
    # bool is an int subclass, but never a count.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def before_send(message: dict[str, Any]) -> dict[str, Any] | None:
    """The SDK's last hook before an event is queued: keeps only the
    event's allowed properties plus `SDK_PROPERTIES`, and drops events
    that aren't ours."""
    try:
        event = Event(message.get("event"))
    except ValueError:
        return None
    properties = message.get("properties") or {}
    message["properties"] = {
        **{name: value for name, value in properties.items() if name in SDK_PROPERTIES},
        **allowed_properties(event, {k: v for k, v in properties.items() if k not in SDK_PROPERTIES}),
    }
    return message


class Analytics:
    """Captures product events. This base class sends nothing (analytics
    off); `PostHogAnalytics` sends them."""

    enabled = False

    def capture(
        self, analytics_id: uuid.UUID, event: Event, properties: Mapping[str, Any] | None = None
    ) -> None:
        """Records that the user with `analytics_id` did `event`. Call it
        once the action is committed. Never raises."""

    def flush(self) -> None:
        """Sends what's queued, within the flush timeout. Never raises."""

    def for_user(self, analytics_id: uuid.UUID) -> "UserAnalytics":
        """Captures for one user, for services that act for them."""
        return UserAnalytics(self, analytics_id)


class UserAnalytics:
    """`Analytics` bound to the user a service acts for."""

    def __init__(self, analytics: Analytics, analytics_id: uuid.UUID):
        self._analytics = analytics
        self._analytics_id = analytics_id

    def capture(self, event: Event, properties: Mapping[str, Any] | None = None) -> None:
        self._analytics.capture(self._analytics_id, event, properties)


class DisabledAnalytics(Analytics):
    pass


class PostHogAnalytics(Analytics):
    enabled = True

    def __init__(self, client: Any):
        """`client`: a configured `posthog.Posthog` (see `build_analytics`)."""
        self._client = client

    def capture(
        self, analytics_id: uuid.UUID, event: Event, properties: Mapping[str, Any] | None = None
    ) -> None:
        try:
            self._client.capture(
                str(event), distinct_id=str(analytics_id), properties=allowed_properties(event, properties)
            )
        except Exception as exc:
            logger.warning("Analytics capture failed", analytics_event=str(event), error_type=type(exc).__name__)

    def flush(self) -> None:
        try:
            self._client.flush(timeout_seconds=get_settings().analytics_flush_timeout_seconds)
        except Exception as exc:
            logger.warning("Analytics flush failed", error_type=type(exc).__name__)


def build_analytics(settings: Settings) -> Analytics:
    """PostHog when it's enabled and configured, otherwise nothing."""
    if not settings.analytics_enabled:
        return DisabledAnalytics()
    if not settings.posthog_project_api_key or not settings.posthog_host:
        logger.warning("Analytics enabled but POSTHOG_PROJECT_API_KEY or POSTHOG_HOST is unset; disabled")
        return DisabledAnalytics()
    try:
        from posthog import Posthog

        client = Posthog(
            settings.posthog_project_api_key,
            host=settings.posthog_host,
            # Queued and sent by the SDK's consumer thread, never inline in
            # a request; flushed explicitly where the process may freeze.
            sync_mode=False,
            max_queue_size=settings.analytics_max_queue_size,
            timeout=settings.analytics_timeout_seconds,
            max_retries=1,
            flush_interval=0.5,
            disable_geoip=True,
            enable_exception_autocapture=False,
            # No feature flags: nothing polls or evaluates them.
            enable_local_evaluation=False,
            before_send=before_send,
        )
    except Exception as exc:
        logger.warning("Analytics client could not be created; disabled", error_type=type(exc).__name__)
        return DisabledAnalytics()
    return PostHogAnalytics(client)


@lru_cache
def get_analytics() -> Analytics:
    """The process's analytics, built once. A FastAPI dependency, so tests
    can override it."""
    return build_analytics(get_settings())
