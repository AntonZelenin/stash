import json
from uuid import UUID

from redis.asyncio import Redis

from stash_shared.queue.base import ItemType, JobQueue, ProcessingJob

_QUEUE_KEY = "stash:item-processing"


class ValkeyJobQueue(JobQueue):
    """`JobQueue` backed by a Valkey list, used as a simple FIFO via
    RPUSH/BLPOP. No redelivery-on-crash or dead-lettering yet — see
    `content_analyzer.worker` for the idempotency/retry behavior that
    compensates for that at the consumer side."""

    def __init__(self, client: Redis, *, queue_key: str = _QUEUE_KEY):
        self._client = client
        self._queue_key = queue_key

    async def publish(self, job: ProcessingJob) -> None:
        await self._client.rpush(self._queue_key, json.dumps(_to_payload(job)))

    async def receive(self, *, timeout_seconds: int) -> ProcessingJob | None:
        result = await self._client.blpop([self._queue_key], timeout=timeout_seconds)
        if result is None:
            return None
        _key, raw = result
        return _from_payload(json.loads(raw))


def _to_payload(job: ProcessingJob) -> dict:
    return {
        "item_id": str(job.item_id),
        "user_id": str(job.user_id),
        "item_type": job.item_type.value,
    }


def _from_payload(data: dict) -> ProcessingJob:
    return ProcessingJob(
        item_id=UUID(data["item_id"]),
        user_id=UUID(data["user_id"]),
        item_type=ItemType(data["item_type"]),
    )


def build_valkey_job_queue(url: str) -> ValkeyJobQueue:
    return ValkeyJobQueue(Redis.from_url(url))
