from types import SimpleNamespace

import pytest

from stash_shared.queue import sqs_queue
from stash_shared.queue.base import THUMBNAIL_JOBS, Delivery, PlatformDeadLetterQueue, RetryMode
from stash_shared.queue.factory import SQS, VALKEY, build_dead_letter_queue, build_job_queue, queue_provider
from stash_shared.queue.sqs_queue import SqsJobQueue
from stash_shared.queue.valkey_queue import ValkeyDeadLetterQueue, ValkeyJobQueue

QUEUE_URL = "https://sqs.eu-west-1.amazonaws.com/123456789012/stash-thumbnail-jobs"


@pytest.fixture(autouse=True)
def aws_region(monkeypatch):
    """What an AWS runtime provides; boto3 needs a region to build a
    client (it connects only when called)."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")


def _settings(**overrides) -> SimpleNamespace:
    """The queue-related fields both services' settings have."""
    fields = {
        "platform": "local",
        "queue_provider": None,
        "valkey_url": "redis://localhost:6379",
        "sqs_queue_urls": {THUMBNAIL_JOBS: QUEUE_URL},
        "queue_visibility_timeout_seconds": 300,
    }
    return SimpleNamespace(**(fields | overrides))


@pytest.mark.parametrize(
    ("platform", "provider"),
    [("local", VALKEY), ("digitalocean", VALKEY), ("aws", SQS)],
)
def test_provider_follows_the_platform(platform, provider):
    assert queue_provider(_settings(platform=platform)) == provider


def test_an_explicit_provider_overrides_the_platform():
    assert queue_provider(_settings(platform="aws", queue_provider=VALKEY)) == VALKEY


def test_local_builds_valkey_queues():
    queue = build_job_queue(_settings(), THUMBNAIL_JOBS)

    assert isinstance(queue, ValkeyJobQueue)
    assert isinstance(build_dead_letter_queue(_settings(), THUMBNAIL_JOBS), ValkeyDeadLetterQueue)


def test_aws_builds_an_sqs_queue_from_the_configured_url():
    queue = build_job_queue(_settings(platform="aws"), THUMBNAIL_JOBS)

    assert isinstance(queue, SqsJobQueue)
    assert queue._queue_url == QUEUE_URL
    assert queue._queue_name == THUMBNAIL_JOBS
    assert queue._visibility_timeout_seconds == 300


def test_aws_uses_the_default_credential_chain(monkeypatch):
    calls = []
    monkeypatch.setattr(sqs_queue.boto3, "client", lambda *args, **kwargs: calls.append((args, kwargs)))

    build_job_queue(_settings(platform="aws"), THUMBNAIL_JOBS)

    # No keys, region or endpoint from settings: on AWS, boto3 takes them
    # from the execution role and the runtime's environment.
    assert calls == [(("sqs",), {})]


def test_a_publisher_without_consumer_settings_gets_the_queue_default_visibility_timeout():
    settings = _settings(platform="aws")
    del settings.queue_visibility_timeout_seconds  # like the API's settings

    queue = build_job_queue(settings, THUMBNAIL_JOBS)

    assert queue._visibility_timeout_seconds is None


def test_aws_without_a_url_for_the_queue_fails_clearly():
    with pytest.raises(ValueError, match="thumbnail_jobs"):
        build_job_queue(_settings(platform="aws", sqs_queue_urls={}), THUMBNAIL_JOBS)


def test_local_retries_after_the_consumers_backoff_and_aws_after_the_visibility_timeout():
    assert build_job_queue(_settings(), THUMBNAIL_JOBS).retry_mode is RetryMode.BACKOFF
    assert build_job_queue(_settings(platform="aws"), THUMBNAIL_JOBS).retry_mode is RetryMode.VISIBILITY_TIMEOUT


def test_aws_leaves_dead_lettering_to_sqs():
    assert isinstance(build_dead_letter_queue(_settings(platform="aws"), THUMBNAIL_JOBS), PlatformDeadLetterQueue)


async def test_valkey_acks_abandoned_deliveries():
    """Its dead letters live in its own dead-letter stream, so a delivery
    given up on is done with."""
    acked = []

    class _Client:
        async def xack(self, stream_key, group, receipt):
            acked.append((stream_key, group, receipt))

    queue = ValkeyJobQueue(_Client(), stream_key="stash:thumbnail_jobs")
    await queue.abandon(Delivery(message_id="1-0", receipt="1-0", delivery_count=5, raw_payload="{}", job=None))

    assert acked == [("stash:thumbnail_jobs", "workers", "1-0")]


def test_an_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="kafka"):
        build_job_queue(_settings(queue_provider="kafka"), THUMBNAIL_JOBS)
    with pytest.raises(ValueError, match="kafka"):
        build_dead_letter_queue(_settings(queue_provider="kafka"), THUMBNAIL_JOBS)
