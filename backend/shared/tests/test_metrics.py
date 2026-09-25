import json
from uuid import UUID

import pytest

from stash_shared import log, metrics
from stash_shared.queue.base import ItemType
from stash_shared.queue.valkey_queue import ValkeyJobQueue


class _Recorder(metrics._Backend):
    def __init__(self):
        self.points: list[tuple[str, str, float, dict[str, str]]] = []

    def record(self, name, unit, value, dimensions, *, summed):
        self.points.append((name, unit.value, value, dict(dimensions)))


@pytest.fixture
def recorded(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(metrics, "_backend", recorder)
    return recorder


@pytest.fixture
def cloudwatch(monkeypatch) -> metrics._CloudWatchBackend:
    """The CloudWatch backend, without its flush thread: tests flush."""
    pytest.importorskip("aws_lambda_powertools")
    backend = metrics._CloudWatchBackend(namespace="Stash", base_dimensions={"service": "api", "environment": "prod"})
    monkeypatch.setattr(metrics, "_backend", backend)
    return backend


def _emf_records(capsys) -> list[dict]:
    metrics.flush()
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.fixture(autouse=True)
def _restore_backend():
    saved = metrics._backend
    yield
    metrics._backend = saved


def test_only_the_aws_platform_publishes(monkeypatch):
    for platform in ("local", "digitalocean"):
        metrics.configure_metrics(service="api", platform=platform, environment="dev")
        assert not metrics.is_enabled()
        metrics.count("Requests", route="/items")  # no-op, no error


def test_aws_platform_publishes_to_cloudwatch(monkeypatch):
    pytest.importorskip("aws_lambda_powertools")
    monkeypatch.setattr(metrics._CloudWatchBackend, "start", lambda self: None)

    metrics.configure_metrics(service="api", platform="AWS", environment="Prod")

    assert metrics.is_enabled()
    assert isinstance(metrics._backend, metrics._CloudWatchBackend)


def test_emf_records_carry_one_dimension_set(cloudwatch, capsys):
    metrics.count("JobFailures", queue="thumbnail_jobs")
    metrics.count("JobFailures", queue="thumbnail_jobs")
    metrics.record_duration("JobDuration", 12.5, queue="thumbnail_jobs")
    metrics.record_duration("JobDuration", 40.0, queue="thumbnail_jobs")

    [record] = _emf_records(capsys)

    [definition] = record["_aws"]["CloudWatchMetrics"]
    assert definition["Namespace"] == "Stash"
    # No extra aggregated copy: every dimension set is a billed metric.
    assert definition["Dimensions"] == [["service", "environment", "queue"]]
    assert {m["Name"]: m["Unit"] for m in definition["Metrics"]} == {
        "JobFailures": "Count",
        "JobDuration": "Milliseconds",
    }
    assert (record["service"], record["environment"], record["queue"]) == ("api", "prod", "thumbnail_jobs")
    # Counts are summed; durations keep every value, for percentiles.
    assert record["JobFailures"] == [2.0]
    assert record["JobDuration"] == [12.5, 40.0]


def test_each_dimension_set_is_its_own_record(cloudwatch, capsys):
    metrics.count("ExternalCallErrors", operation="storage.upload")
    metrics.count("ExternalCallErrors", operation="openai.embeddings")
    metrics.gauge("QueueBacklog", 3, unit=metrics.Unit.COUNT)

    records = _emf_records(capsys)

    assert [(r.get("operation"), r.get("ExternalCallErrors"), r.get("QueueBacklog")) for r in records] == [
        ("storage.upload", [1.0], None),
        ("openai.embeddings", [1.0], None),
        (None, None, [3.0]),
    ]
    [backlog] = [r for r in records if "QueueBacklog" in r]
    assert backlog["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["service", "environment"]]


def test_many_values_are_split_across_records(cloudwatch, capsys):
    for value in range(250):
        metrics.record_duration("JobDuration", value, queue="thumbnail_jobs")
    metrics.count("JobFailures", 250, queue="thumbnail_jobs")

    records = _emf_records(capsys)

    assert [len(r["JobDuration"]) for r in records] == [99, 99, 52]
    assert [value for r in records for value in r["JobDuration"]] == [float(v) for v in range(250)]
    assert sum(sum(r.get("JobFailures", [])) for r in records) == 250


def test_flushing_empties_the_buffer(cloudwatch, capsys):
    metrics.count("Requests")
    assert len(_emf_records(capsys)) == 1
    assert _emf_records(capsys) == []


def test_dimensions_are_strings_and_none_is_dropped(recorded):
    metrics.count("Requests", route=None, item_type=ItemType.image, attempt=2)

    assert recorded.points == [("Requests", "Count", 1.0, {"item_type": "image", "attempt": "2"})]


def test_recording_never_raises(monkeypatch):
    class _Broken(metrics._Backend):
        def record(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(metrics, "_backend", _Broken())

    metrics.count("Requests")
    metrics.record_duration("RequestDuration", "not a number")


async def test_external_call_records_errors_and_duration(recorded):
    with metrics.external_call("storage.upload"):
        pass
    with pytest.raises(TimeoutError):
        with metrics.external_call("storage.upload"):
            raise TimeoutError

    # Every call has a duration (its SampleCount is the call count).
    names = [(name, dims["operation"]) for name, _unit, _value, dims in recorded.points]
    assert names == [
        ("ExternalCallDuration", "storage.upload"),
        ("ExternalCallErrors", "storage.upload"),
        ("ExternalCallDuration", "storage.upload"),
    ]


async def test_logged_call_is_measured(recorded):
    with pytest.raises(ValueError):
        with log.logged_call(log.get_logger("test"), "openai.embeddings", model="m", item_id=UUID(int=1)):
            raise ValueError

    # Only `operation`: the call's log fields (ids included) never become dimensions.
    assert {name: dims for name, _unit, _value, dims in recorded.points} == {
        "ExternalCallErrors": {"operation": "openai.embeddings"},
        "ExternalCallDuration": {"operation": "openai.embeddings"},
    }


# ---- Valkey queue stats ---------------------------------------------------------


class _FakeStreamRedis:
    """A stream's consumer-group bookkeeping, as redis-py returns it."""

    def __init__(self, *, pending: int, lag: int | None, oldest_pending: str | None, first_undelivered: str | None):
        self._pending, self._lag = pending, lag
        self._oldest_pending, self._first_undelivered = oldest_pending, first_undelivered
        self.xrange_min: str | None = None

    async def xgroup_create(self, *args, **kwargs):
        return True

    async def xinfo_groups(self, stream_key):
        return [
            {"name": "other", "pending": 99, "lag": 99, "last-delivered-id": "0-0"},
            {"name": "workers", "pending": self._pending, "lag": self._lag, "last-delivered-id": "5000-0"},
        ]

    async def xpending(self, stream_key, group):
        return {"pending": self._pending, "min": self._oldest_pending, "max": None, "consumers": []}

    async def xrange(self, stream_key, min, max, count):
        self.xrange_min = min
        return [(self._first_undelivered, {})] if self._first_undelivered else []

    async def time(self):
        return (70, 500_000)  # 70.5s after the epoch


async def test_valkey_stats_count_undelivered_and_pending_messages():
    redis = _FakeStreamRedis(pending=2, lag=3, oldest_pending="10000-0", first_undelivered="40000-1")

    stats = await ValkeyJobQueue(redis, stream_key="stash:jobs").stats()

    assert stats.backlog == 5
    # The oldest of both: the pending one published at 10s.
    assert stats.oldest_message_age_seconds == pytest.approx(60.5)
    # Undelivered means after the group's last delivered entry.
    assert redis.xrange_min == "(5000-0"


async def test_valkey_stats_of_an_empty_queue():
    redis = _FakeStreamRedis(pending=0, lag=0, oldest_pending=None, first_undelivered=None)

    stats = await ValkeyJobQueue(redis, stream_key="stash:jobs").stats()

    assert (stats.backlog, stats.oldest_message_age_seconds) == (0, 0.0)


async def test_valkey_stats_when_the_lag_is_unknown():
    redis = _FakeStreamRedis(pending=0, lag=None, oldest_pending=None, first_undelivered="70000-0")

    stats = await ValkeyJobQueue(redis, stream_key="stash:jobs").stats()

    assert stats.backlog is None
    assert stats.oldest_message_age_seconds == pytest.approx(0.5)
